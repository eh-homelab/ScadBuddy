// Assembles a turn's `query()` options (unified runtime spec 2026-10-13 §5.3,
// plan Task 1.2): what `SessionManager.runTurn` used to build inline. Every
// option here is fixed at spawn, so none of its callbacks holds the turn: the
// gates, the hooks, the in-process servers' turn context and tab wait, and the
// http tool's turn id and signal all read the current turn from a TurnSlot
// (turnSlot.ts) and fail closed without one. `runTurn` sets the slot, adds the
// prompt and the abort signal, and runs the query; for now still one per turn.

import path from 'node:path'
import type { SDKMessage, SessionStore } from '@anthropic-ai/claude-agent-sdk'
import { ROOT_CONTEXT } from '@opentelemetry/api'
import type { PooledCredential } from '../harness/fallback.js'
import {
  HTTP_SERVER,
  httpRequestEnabled,
  httpRequestServer,
  httpTierOf,
  SETTING_HTTP_REQUEST,
} from '../harness/httpRequest.js'
import type { RiskTier, TierResolver } from '../harness/permissions.js'
import { isQuestionTool } from '../harness/questions.js'
import { harnessTierOf, type HarnessRun } from '../harness/run.js'
import { ensureSessionDir } from '../harness/stateDirs.js'
import { createMemoryHooks, HindsightClient, hindsightTarget, type MemoryHooks, turnRecallMaxQueryChars } from '../memory/hindsight.js'
import type { BuiltInPolicy } from '../plugins/builtInTools.js'
import type { PluginEndpoint, PluginsForRun } from '../plugins/forwarder.js'
import { ownPluginEnabled } from '../plugins/packages/builtins.js'
import type { PackagesForRun } from '../plugins/packages/install.js'
import { type PackageServers, withoutRemoteServers } from '../plugins/packages/mcp.js'
import type { CheckedPlugin } from '../plugins/registry.js'
import { TurnTrace } from '../telemetry/turn.js'
import type { SessionFiles } from '../workspace/store.js'
import type { EventLog } from './eventLog.js'
import {
  type ClaimedSession,
  recordMemoryActivity,
  type SessionManagerDeps,
  type SessionRecord,
  SETTING_MODEL,
  systemAppend,
  type TurnPrincipal,
} from './manager.js'
import { event } from './protocol.js'
import { scrubForLog } from './sdkEvents.js'
import type { PostgresSessionStore } from './store.js'
import { NO_TURN, slotHooks, slotHttpContext, type TurnSlot } from './turnSlot.js'

export type TurnRunnerDeps = Pick<
  SessionManagerDeps,
  | 'paths'
  | 'credentials'
  | 'settings'
  | 'tierOf'
  | 'mcpServers'
  | 'builtInTools'
  | 'pluginPaths'
  | 'ownPlugin'
  | 'remotePlugins'
  | 'packagePlugins'
  | 'packageServers'
  | 'memory'
  | 'httpRequest'
  | 'audit'
  | 'stderr'
> & {
  /** The transcript mirror (store.ts): the run's `sessionStore`, and whether a session has a transcript to resume. */
  store: SessionStore & Pick<PostgresSessionStore, 'exists'>
  events: Pick<EventLog, 'append'>
  /** The workspace http_request's `save_to_workspace` writes to (#2329). */
  files: SessionFiles
}

/** Everything fixed at spawn that decides whether a warm process may serve a turn (spec §5.3 "Reuse rule"). */
export type FingerprintParts = {
  credentialId: string
  credentialUpdatedAt: string
  model: string | null
  ownPlugin: boolean
  /** Each plugin's configured endpoint, header version, tool tiers and disabled tools (forwarder.ts `pluginEndpoint`). */
  plugins: PluginEndpoint[]
  packageHashes: string[]
  /** The packages approved with `allow_refused`, loaded unchecked with their built-ins (#297). */
  allowedPackageHashes: string[]
  tiers: string[] | null
  owner: string
  systemAppend: string
  disabledTools: string[]
  /** Settings → Plugins' raised tiers (#1953), `<harness name>=<tier>`, sorted. */
  raisedTiers: string[]
  /** Whether the run has the http_request tool (#827). */
  httpRequest: boolean
  builtinTools: string[]
  /**
   * The memory hooks' recall query cap (#2655, Settings `memory_recall_max_query_chars`),
   * read at assembly; null when the run has no memory hooks.
   */
  recallMaxQueryChars: number | null
}

export type AssembledTurn = {
  run: Omit<HarnessRun, 'prompt' | 'credential' | 'signal'>
  candidates: PooledCredential[]
  fingerprintParts: FingerprintParts
  /**
   * Redacted from everything the turn writes to the durable event log: every
   * candidate's secret, since the turn may fall back to any of them, and the
   * plugins' header values (and their bare tokens).
   */
  secrets: string[]
  /** Each tool's tier as the permission seam applies it, plugins' and Settings → Plugins' raised tiers included. */
  eventTierOf: TierResolver
  /** As `eventTierOf`, but AskUserQuestion and ask_user are `read` where the user is asked (#940): what the panel shows and audits. */
  shownTierOf: TierResolver
  /** Checks Claude Code's init message for a plugin or in-process server it did not load, and says so in the session. */
  observe(message: SDKMessage): Promise<void>
  /** The turn's automatic memory hooks, recalled against `userText`; only when a Hindsight plugin is loaded. */
  memory?: (userText: string, turnId: string) => MemoryHooks
  /** Releases the plugins' forwarder routes and the packages' directories: when the turn has ended. */
  release(): void
}

/** Thrown by `assemble`: the failure, and the secrets read before it, which its message is redacted of. */
export class TurnAssemblyError extends Error {
  override name = 'TurnAssemblyError'
  override readonly cause: unknown
  readonly secrets: string[]
  constructor(cause: unknown, secrets: string[]) {
    super(cause instanceof Error ? cause.message : String(cause))
    this.cause = cause
    this.secrets = secrets
  }
}

type RunKey = keyof AssembledTurn['run']

export class TurnRunner {
  /** Every key `assemble` may give `run`: each is fixed for the life of the process it spawns. */
  static readonly FIXED_AT_SPAWN: readonly RunKey[] = [
    'paths',
    'cwd',
    'sessionStore',
    'includePartialMessages',
    'maxTurns',
    'maxBudgetUsd',
    'tierOf',
    'builtInPolicy',
    'approvalGate',
    'questionGate',
    'systemPromptAppend',
    'resume',
    'sessionId',
    'model',
    'mcpServers',
    'pluginPaths',
    'allowedPluginPaths',
    'builtinTools',
    'ownPlugin',
    'remotePlugins',
    'memoryHooks',
    'traceHooks',
    'stderr',
  ]

  /**
   * What decides each `FIXED_AT_SPAWN` key for the warm cache (warm.ts): the
   * `FingerprintParts` field that moves with it, `slot` for a callback that
   * reads the TurnSlot (so a turn's own state never needs a respawn), or `spawn`
   * for a value the cache sets per spawn and checks by its own rule (§5.3
   * "Reuse rule": fingerprint, transcript tail, budget).
   */
  static readonly FINGERPRINTED_BY: Readonly<Record<string, keyof FingerprintParts | 'slot' | 'spawn'>> = {
    paths: 'spawn', // fixed config of the service: the same for every process it spawns (fingerprint rule)
    cwd: 'spawn', // the session's own directory: a warm process is per session (fingerprint rule)
    sessionStore: 'spawn', // the service's one transcript mirror (transcript tail rule)
    includePartialMessages: 'spawn', // a constant (fingerprint rule)
    maxTurns: 'spawn', // the session row's cap, fixed for the session (fingerprint rule)
    maxBudgetUsd: 'spawn', // what is left of the budget at spawn: its own reuse check (budget rule)
    tierOf: 'plugins', // the registry's tiers, widened by each plugin's `toolTiers`
    builtInPolicy: 'raisedTiers', // Settings -> Plugins' raised tiers and disabled built-ins (`disabledTools`)
    approvalGate: 'slot',
    questionGate: 'slot', // present only for a browser-owned session, which `owner` fingerprints
    systemPromptAppend: 'systemAppend',
    resume: 'spawn', // resume or new id: the transcript tail rule
    sessionId: 'spawn', // as `resume` (transcript tail rule)
    model: 'model',
    mcpServers: 'tiers', // the in-process servers depend on the principal's tiers (and `owner`, `httpRequest`)
    pluginPaths: 'packageHashes',
    allowedPluginPaths: 'allowedPackageHashes',
    builtinTools: 'builtinTools',
    ownPlugin: 'ownPlugin',
    remotePlugins: 'plugins',
    memoryHooks: 'slot',
    traceHooks: 'slot',
    stderr: 'spawn', // a constant sink (fingerprint rule)
  }

  private readonly deps: TurnRunnerDeps

  constructor(deps: TurnRunnerDeps) {
    this.deps = deps
  }

  async assemble(session: ClaimedSession, principal: TurnPrincipal, slot: TurnSlot): Promise<AssembledTurn> {
    const id = session.id
    const registryTierOf = this.deps.tierOf ?? (() => undefined)
    // The http tools' tier depends on the method in their input (#827).
    const tierOf: TierResolver = (name, input) => httpTierOf(name, input) ?? registryTierOf(name, input)
    // Widened with the plugins' tiers once they are loaded below, so the
    // panel shows a plugin tool at the tier the permission seam applies.
    // Settings → Plugins' raised tiers (#1953), once read below, over every tier shown.
    let builtIn: BuiltInPolicy | undefined
    const raised = (name: string, tier: RiskTier | undefined) => (tier === undefined || !builtIn ? tier : builtIn.tierOf(name, tier))
    let eventTierOf: TierResolver = (name, input) => raised(name, tierOf(name, input))
    // AskUserQuestion and ask_user (#940) only ask the user: shown and audited as `read`, as the harness tiers them.
    const asksUser = session.owner.kind === 'browser'
    const shownTierOf: TierResolver = (name, input) =>
      asksUser && isQuestionTool(name) ? 'read' : eventTierOf(name, input)

    /** Redacted from everything this turn writes to the durable event log. */
    let secrets: string[] = []
    let forwarded: PluginsForRun | undefined
    let packages: PackagesForRun | undefined
    let packageServers: PackageServers | undefined
    const release = () => {
      forwarded?.release()
      packageServers?.release()
      packages?.release()
    }
    try {
      // The credentials first, and into `secrets` at once: whatever fails
      // after this point is redacted before it reaches the event log. Every
      // candidate's, since the turn may fall back to any of them.
      const candidates = await this.deps.credentials.candidates()
      secrets = candidates.map((c) => c.credential.secret)
      builtIn = this.deps.builtInTools ? await this.deps.builtInTools() : undefined
      forwarded = this.deps.remotePlugins ? await this.deps.remotePlugins() : undefined
      const remotePlugins = [...(forwarded?.plugins ?? [])]
      // Plugin header values (and their bare tokens) are redacted from the
      // event log like the credential. Claude Code never holds them (the
      // forwarder adds them), but a plugin could echo one in a tool result.
      secrets.push(...(forwarded?.secrets ?? []))
      const memory = forwarded?.hindsight ? await this.memoryHooks(forwarded.hindsight, secrets, session) : undefined
      // Settings → plugin packages may switch it off (plugins/packages/builtins.ts).
      const ownPlugin =
        this.deps.ownPlugin !== undefined && (await ownPluginEnabled(this.deps.settings))
          ? this.deps.ownPlugin
          : undefined
      packages = this.deps.packagePlugins ? await this.deps.packagePlugins() : undefined
      // Each loaded directory's package, by the path Claude Code is handed (a stripped copy's too).
      const packageNames = new Map<string, string>()
      if (packages) {
        // Loaded without their own http MCP servers, which come through the forwarder
        // below: a query with strictMcpConfig off would start them itself (run.ts).
        const loaded = packages
        const problems = [...loaded.problems]
        // A package that cannot be stripped is left out and said, as one that cannot be loaded.
        const stripped = async (dir: string): Promise<string | undefined> => {
          const pkg = loaded.loaded.find((p) => p.dir === dir)
          if (!pkg) return dir
          try {
            const loadedDir = await withoutRemoteServers(dir, pkg.contentHash, path.join(this.deps.paths.stateDir, 'plugins-local'))
            packageNames.set(loadedDir, pkg.name)
            return loadedDir
          } catch (err) {
            problems.push(`plugin package ${pkg.name} was not loaded: ${(err as Error).message}`)
            return undefined
          }
        }
        const kept = async (dirs: readonly string[]) => (await Promise.all(dirs.map(stripped))).filter((d): d is string => d !== undefined)
        packages = { ...loaded, paths: await kept(loaded.paths), allowedPaths: await kept(loaded.allowedPaths), problems }
      }
      // Every package's http MCP servers, through the forwarder like a remote
      // plugin's (#2320); Claude Code loads the packages without them (above).
      packageServers =
        packages && this.deps.packageServers
          ? await this.deps.packageServers(packages.loaded)
          : undefined
      remotePlugins.push(...(packageServers?.plugins ?? []))
      secrets.push(...(packageServers?.secrets ?? []))
      const pluginTiers = harnessTierOf({
        remotePlugins,
        tierOf,
        ...(ownPlugin !== undefined ? { ownPlugin } : {}),
        pluginPaths: [...(this.deps.pluginPaths ?? []), ...(packages?.paths ?? [])],
        allowedPluginPaths: packages?.allowedPaths ?? [],
      })
      eventTierOf = (name, input) => raised(name, pluginTiers(name, input))
      // A plugin left out of this turn is said so in the session, not only in the log.
      const unavailable = (message: string) =>
        this.deps.events.append(id, [
          scrubForLog(event({ type: 'error', sessionId: id, code: 'plugin_unavailable', message }), secrets),
        ])
      for (const problem of forwarded?.problems ?? []) {
        this.deps.stderr?.(`${problem}\n`)
        await unavailable(problem)
      }
      for (const problem of [...(packages?.problems ?? []), ...(packageServers?.problems ?? [])]) {
        this.deps.stderr?.(`${problem}\n`)
        await unavailable(problem)
      }
      const pluginPaths = [...(this.deps.pluginPaths ?? []), ...(packages?.paths ?? [])]
      const allowedPluginPaths = packages?.allowedPaths ?? []
      const loadedPackages = packages
      /** The in-process MCP servers this turn gives Claude Code (`run.mcpServers`), for pluginCheck. */
      let inProcessServers: string[] = []
      const pluginCheck = async (message: SDKMessage) => {
        if (message.type !== 'system' || message.subtype !== 'init') return
        // The SDK skips a plugin it cannot load; the init message lists what it did load
        // (https://code.claude.com/docs/en/agent-sdk/plugins, "Verifying plugin installation").
        const listed = (message as { plugins?: { path: string }[] }).plugins ?? []
        const loaded = new Set(listed.map((p) => path.resolve(p.path)))
        if (ownPlugin !== undefined && !loaded.has(path.resolve(ownPlugin))) {
          await unavailable("ScadBuddy's own plugin was not loaded by Claude Code: its skills and subagents are unavailable")
        }
        for (const dir of [...(loadedPackages?.paths ?? []), ...allowedPluginPaths]) {
          if (!loaded.has(path.resolve(dir))) {
            await unavailable(`plugin package ${packageNames.get(dir) ?? path.basename(path.dirname(dir))} was not loaded by Claude Code`)
          }
        }
        // #1255: Claude Code drops an in-process server it cannot connect, and every
        // tool on it, logging that at debug level only. Say so in the session.
        for (const name of inProcessServers) {
          const status = message.mcp_servers.find((s) => s.name === name)?.status
          if (status !== 'connected') {
            await this.deps.events.append(id, [
              scrubForLog(
                event({
                  type: 'error',
                  sessionId: id,
                  code: 'tools_unavailable',
                  message: `the ${name} tools are not available in this turn: Claude Code reports their in-process MCP server as ${status ?? 'missing'}`,
                }),
                secrets,
              ),
            ])
          }
        }
        for (const plugin of remotePlugins) {
          const status = message.mcp_servers.find((s) => s.name === plugin.name)?.status
          if (status !== 'connected') {
            await unavailable(
              `plugin ${plugin.name} is not available in this turn: its MCP server is ${status ?? 'missing'}`,
            )
          }
        }
      }
      const [cwd, resume, model, httpSetting] = await Promise.all([
        ensureSessionDir(this.deps.paths, id),
        this.deps.store.exists(id),
        this.deps.settings?.get<string>(SETTING_MODEL),
        this.deps.httpRequest ? this.deps.settings?.get<unknown>(SETTING_HTTP_REQUEST) : undefined,
      ])
      // The http_request tool (#827): on unless the setting is `false`. Its
      // saved bodies live in the session's own directory, and it never sees
      // the turn's secrets except to refuse a request that carries one.
      const http =
        this.deps.httpRequest && httpRequestEnabled(httpSetting)
          ? httpRequestServer(
              slotHttpContext(
                {
                  saveDir: path.join(cwd, 'http'),
                  // save_to_workspace (#2329): the body becomes one of the session's files.
                  workspace: { files: this.deps.files, ref: { kind: 'session', sessionId: id } },
                  secrets: () => secrets,
                  audit: this.deps.audit,
                  actor: session.owner,
                  sessionId: id,
                  ...(this.deps.httpRequest.resolve ? { resolve: this.deps.httpRequest.resolve } : {}),
                  ...(this.deps.httpRequest.limits ? { limits: this.deps.httpRequest.limits } : {}),
                },
                slot,
              ),
            )
          : undefined
      const run: AssembledTurn['run'] = {
        paths: this.deps.paths,
        cwd,
        sessionStore: this.deps.store,
        includePartialMessages: true,
        maxTurns: session.maxTurns,
        maxBudgetUsd: Math.max(session.budgetUsd - session.costUsd, 0.000001),
        tierOf,
        ...(builtIn ? { builtInPolicy: builtIn } : {}),
        approvalGate: (request) => {
          const turn = slot.current
          return turn ? turn.approvalGate(request) : Promise.resolve({ approved: false, message: NO_TURN })
        },
        // AskUserQuestion (#940): only the user in the panel answers, so only a
        // session the browser user owns is given the tool. Any other owner's turn
        // would wait on someone who is not asked.
        ...(asksUser
          ? {
              questionGate: (request) => {
                const gate = slot.current?.questionGate
                return gate ? gate(request) : Promise.resolve({ answered: false, message: NO_TURN })
              },
            }
          : {}),
        systemPromptAppend: systemAppend(),
        // First turn: the SDK session gets OUR id; later turns resume it.
        ...(resume ? { resume: id } : { sessionId: id }),
        ...(typeof model === 'string' && model ? { model } : {}),
        ...(this.deps.mcpServers || http
          ? {
              mcpServers: {
                ...(this.deps.mcpServers
                  ? this.deps.mcpServers(session, principal, {
                      ...(asksUser
                        ? {
                            waitForTab: (request) => {
                              const wait = slot.current?.waitForTab
                              return wait ? wait(request) : Promise.resolve({ back: false, message: NO_TURN })
                            },
                          }
                        : {}),
                      turnContext: () => slot.current?.trace.context() ?? ROOT_CONTEXT,
                      ...(builtIn ? { builtIn } : {}),
                      // The turn's credentials and plugin tokens, which workspace_read redacts (#2329).
                      secrets: () => secrets,
                    })
                  : {}),
                ...(http ? { [HTTP_SERVER]: http } : {}),
              },
            }
          : {}),
        ...(pluginPaths.length ? { pluginPaths } : {}),
        ...(allowedPluginPaths.length ? { allowedPluginPaths, builtinTools: packages?.builtinTools ?? [] } : {}),
        ...(ownPlugin !== undefined ? { ownPlugin } : {}),
        ...(remotePlugins.length ? { remotePlugins } : {}),
        // Shaped like any turn's memory hooks (the same config gives the same
        // events, matchers and timeouts); each runs the current turn's own.
        ...(memory ? { memoryHooks: slotHooks(memory.hooks('', '').hooks, slot, (turn) => turn.memory?.hooks) } : {}),
        traceHooks: TurnTrace.hooksFor(() => slot.current?.trace),
        ...(this.deps.stderr ? { stderr: this.deps.stderr } : {}),
      }
      inProcessServers = Object.keys(run.mcpServers ?? {})
      const first = candidates[0]
      const fingerprintParts: FingerprintParts = {
        credentialId: first?.id ?? '',
        // The row's epoch moves with every new secret and reset (credentials.ts).
        credentialUpdatedAt: first ? String(first.epoch) : '',
        model: run.model ?? null,
        ownPlugin: ownPlugin !== undefined,
        // Each plugin's configured endpoint and header version, never the loopback
        // URL this assembly registered (forwarder.ts `pluginEndpoint`).
        plugins: [...(forwarded?.endpoints ?? []), ...(packageServers?.endpoints ?? [])],
        packageHashes: (packages?.loaded ?? []).map((p) => p.contentHash),
        allowedPackageHashes: (packages?.loaded ?? []).filter((p) => p.allowed).map((p) => p.contentHash),
        tiers: principal.tiers ? [...principal.tiers] : null,
        owner: `${session.owner.kind}:${session.owner.id}`,
        systemAppend: run.systemPromptAppend ?? '',
        // Settings → Plugins' disabled built-ins; each plugin's own are in its `plugins` entry.
        disabledTools: [...(builtIn?.disallowed ?? [])],
        raisedTiers: [...(builtIn?.raised ?? [])],
        httpRequest: http !== undefined,
        builtinTools: run.builtinTools ?? [],
        recallMaxQueryChars: memory?.recallMaxQueryChars ?? null,
      }
      return {
        run,
        candidates,
        fingerprintParts,
        secrets,
        eventTierOf: (name, input) => eventTierOf(name, input),
        shownTierOf,
        observe: pluginCheck,
        ...(memory ? { memory: memory.hooks } : {}),
        release,
      }
    } catch (err) {
      release()
      throw new TurnAssemblyError(err, secrets)
    }
  }

  /**
   * The turn's memory hooks, for the `hindsight` plugin this turn loaded (so
   * only an enabled plugin whose endpoint passed its egress check this turn).
   * Recall is against the user's words (`recallQuery`), not the page context
   * appended after them, unless a fixed query is configured. Undefined when
   * memory is off or the plugin's URL is not a bank endpoint (logged).
   *
   * Each recall and retain becomes an audit row and a `memory` panel event
   * (#818; hooks make no SDK message, so TurnAuditor never sees them). A
   * retain reports after the turn ended; both writes work then too, since
   * they need only the session row. A failure is logged by the hooks.
   *
   * The recall query's cap (`recallMaxQueryChars`, #2655) is read from Settings
   * here, at each assembly, and is fingerprinted, so a change reaches the next turn.
   */
  private async memoryHooks(
    plugin: CheckedPlugin,
    secrets: readonly string[],
    session: SessionRecord,
  ): Promise<{ hooks: (userText: string, turnId: string) => MemoryHooks; recallMaxQueryChars: number } | undefined> {
    const config = this.deps.memory
    if (config === false) return undefined
    const log = (line: string) => this.deps.stderr?.(line)
    let client: HindsightClient
    try {
      client = new HindsightClient(hindsightTarget(plugin))
    } catch (err) {
      log(`hindsight: automatic memory is off for this turn: ${err instanceof Error ? err.message : String(err)}\n`)
      return undefined
    }
    const fixed = config?.hookConfig?.recallQuery
    const recallMaxQueryChars = await turnRecallMaxQueryChars(this.deps.settings, config?.hookConfig)
    const hooks = (userText: string, turnId: string) =>
      createMemoryHooks({
        ...config,
        hookConfig: { ...config?.hookConfig, recallQuery: fixed && fixed !== '$prompt' ? fixed : userText, recallMaxQueryChars },
        client,
        secrets,
        log,
        sessionStore: this.deps.store,
        // One recall or retain: its audit row (never fails, audit/log.ts) and its panel event.
        onActivity: (activity) =>
          recordMemoryActivity({ audit: this.deps.audit, events: this.deps.events }, session, turnId, activity, secrets),
      })
    return { hooks, recallMaxQueryChars }
  }
}
