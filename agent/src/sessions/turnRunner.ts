// Assembles a turn's `query()` options (unified runtime spec 2026-10-13 §5.3,
// plan Task 1.2): what `SessionManager.runTurn` used to build inline. Every
// option here is fixed at spawn, so none of its callbacks holds the turn: the
// gates, the hooks, the in-process servers' turn context and tab wait, and the
// http tool's turn id and signal all read the current turn from the assembly's
// own TurnSlot (turnSlot.ts, `AssembledTurn.slot`) and fail closed without one.
// The warm cache (warm.ts) spawns a process from an assembly and keeps the
// assembly with it; each turn that process serves sets that assembly's slot
// before it pushes its prompt (sessions/warmTurn.ts) and clears it when it ends.

import path from 'node:path'
import type { SDKMessage, SessionStore } from '@anthropic-ai/claude-agent-sdk'
import { type Context, ROOT_CONTEXT } from '@opentelemetry/api'
import { NoUsableCredentialError, type PooledCredential } from '../harness/fallback.js'
import { type AccessRow, BUILTIN_SOURCES, packageServerSource, packageSource, type ToolAccess } from '../access/model.js'
import { type CallPlugin, offCallItem, subagentItem } from '../access/items.js'
import type { PackageAccess } from '../access/resolver.js'
import { DEFAULT_ACCESS } from '../access/resolve.js'
import { liveAccess } from '../access/live.js'
import { SeamLedger } from '../harness/seamLedger.js'
import { HTTP_SERVER, HTTP_TOOL, httpAccessTierOf, httpRequestServer, httpTierOf } from '../harness/httpRequest.js'
import type { RiskTier, TierResolver } from '../harness/permissions.js'
import { isQuestionTool } from '../harness/questions.js'
import { harnessTierOf, type HarnessRun } from '../harness/run.js'
import { ensureSessionDir } from '../harness/stateDirs.js'
import { createMemoryHooks, HindsightClient, hindsightTarget, type MemoryHooks, turnRecallMaxQueryChars } from '../memory/hindsight.js'
import type { BuiltInPolicy } from '../plugins/builtInTools.js'
import type { PluginEndpoint, PluginsForRun } from '../plugins/forwarder.js'
import type { PackagesForRun } from '../plugins/packages/install.js'
import { builtinsOffOf, derivedPackage, omitsAny, omittedOf, packageOmitted } from '../plugins/packages/derived.js'
import { collidingServerNames, localServers, type PackageServers, serverAccess, type ServerAccess, unofferableNameProblem, unofferedLocalServers } from '../plugins/packages/mcp.js'
import { callPluginOf, itemsOfPlugin, OWN_PLUGIN_NAME, type PackageItems } from '../plugins/packages/vet.js'
import { harnessToolName, toolPrefix } from '../plugins/registry.js'
import type { CheckedPlugin } from '../plugins/registry.js'
import { withSpan } from '../telemetry/trace.js'
import { TurnTrace } from '../telemetry/turn.js'
import type { SessionFiles } from '../workspace/store.js'
import type { EventLog } from './eventLog.js'
import type { ClaimedSession, SessionManagerDeps, SessionRecord, TurnPrincipal } from './manager.js'
import { event } from './protocol.js'
import { scrubForLog } from './sdkEvents.js'
import type { PostgresSessionStore } from './store.js'
import { poolRemainingUsd, recordMemoryActivity, SETTING_MODEL, systemAppend } from './turnShared.js'
import { createTurnSlot, NO_TURN, slotHooks, slotHttpContext, slotWebContext, type TurnContext, type TurnSlot } from './turnSlot.js'
import { PgHttpBodies } from './httpBodies.js'
import { nestedQueries } from '../builtins/nested.js'
import { browserGuard } from '../browser/guard.js'
import { BrowserOrigins } from '../browser/store.js'
import { loadAgentDefinitions } from '../harness/agentDefinitions.js'
import { BACKGROUND_BUILTINS, BUILTIN_SERVER, CLASSIC_BUILTINS, classicBuiltinNote, classicBuiltinServer } from '../builtins/server.js'

export type TurnRunnerDeps = Pick<
  SessionManagerDeps,
  | 'paths'
  | 'credentials'
  | 'settings'
  | 'access'
  | 'tools'
  | 'tierOf'
  | 'mcpServers'
  | 'prepareApproval'
  | 'builtInTools'
  | 'pluginPaths'
  | 'ownPlugin'
  | 'remotePlugins'
  | 'packagePlugins'
  | 'packageServers'
  | 'memory'
  | 'httpRequest'
  | 'sql'
  | 'background'
  | 'nestedQueries'
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
  /**
   * The pool's first candidate, id and epoch (#3451): enough, because the credential a
   * process runs on is checked apart from the fingerprint. warm.ts compares the hash of
   * the key it was spawned with (`mismatch`), so a fallback whose secret rotated is not
   * reused, and `revalidate` looks its candidate's id and epoch up in the pool again.
   */
  credentialId: string
  credentialUpdatedAt: string
  model: string | null
  /** A Workflow agent's chat (background/workflowAgents.ts): its own effort and subagent definition. */
  effort: string | null
  agentType: string | null
  /** The own plugin's loaded path (its derived copy's when something in it is off, #4757); null when not loaded. */
  ownPlugin: string | null
  /** Each plugin's configured endpoint, header version, tool tiers and disabled tools (forwarder.ts `pluginEndpoint`). */
  plugins: PluginEndpoint[]
  packageHashes: string[]
  /** The packages approved with `allow_refused`, loaded unchecked with their built-ins (#297). */
  allowedPackageHashes: string[]
  tiers: string[] | null
  owner: string
  systemAppend: string
  disabledTools: string[]
  /** Settings → Tools & plugins' raised tiers (#1953), `<harness name>=<tier>`, sorted. */
  raisedTiers: string[]
  /** Whether the run has the http_request tool (#827). */
  httpRequest: boolean
  /** http_request's stored access in ai_access (#2171), its tier when one is set; null when none is stored. */
  httpTier: ToolAccess | null
  builtinTools: string[]
  /** The package built-ins the session serves in place of Claude Code's (#2684): background ones and WebFetch. */
  servedBuiltins: string[]
  /** Whether the session keeps background tasks (src/background/), which decides which built-ins it serves. */
  backgroundTasks: boolean
  /**
   * The memory hooks' recall query cap (#2655, Settings `memory_recall_max_query_chars`),
   * read at assembly; null when the run has no memory hooks.
   */
  recallMaxQueryChars: number | null
  /**
   * `maxRetries` (CLAUDE_CODE_MAX_RETRIES, in the process's env): what the
   * fallback bounds Claude Code's retries to on a credential that has another
   * behind it (harness/fallback.ts), set per attempt by sessions/warmTurn.ts;
   * null when unbounded. `assemble` leaves it null.
   */
  transientRetries: number | null
}

export type AssembledTurn = {
  run: Omit<HarnessRun, 'prompt' | 'credential' | 'signal'>
  candidates: PooledCredential[]
  fingerprintParts: FingerprintParts
  /** What the run's callbacks read as the current turn: set by each turn the assembly's process serves. */
  slot: TurnSlot
  /**
   * Redacted from everything the turn writes to the durable event log: every
   * candidate's secret, since the turn may fall back to any of them, and the
   * plugins' header values (and their bare tokens).
   */
  secrets: string[]
  /** Each tool's tier as the permission seam applies it, plugins' and Settings → Tools & plugins' raised tiers included. */
  eventTierOf: TierResolver
  /** As `eventTierOf`, but AskUserQuestion and ask_user are `read` where the user is asked (#940): what the panel shows and audits. */
  shownTierOf: TierResolver
  /** Checks Claude Code's init message for a plugin or in-process server it did not load, and says so in the session. */
  observe(message: SDKMessage): Promise<void>
  /** The turn's automatic memory hooks, recalled against `userText`; only when a Hindsight plugin is loaded. */
  memory?: (userText: string, turnId: string) => MemoryHooks
  /**
   * Ends the upstream MCP sessions the plugins' routes saw, then releases the routes: Claude
   * Code sends no session DELETE when its process ends (spec 2026-10-13 §5.3 E7), so the
   * owner of a process that has ended calls this before `release`. Bounded (5 s); never rejects.
   */
  terminate(): Promise<void>
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
    'maxRetries',
    'tierOf',
    'browser',
    'builtInPolicy',
    'live',
    'approvalGate',
    'prepareApproval',
    'questionGate',
    'systemPromptAppend',
    'resume',
    'sessionId',
    'model',
    'effort',
    'agent',
    'mcpServers',
    'pluginPaths',
    'allowedPluginPaths',
    'builtinTools',
    'ownPlugin',
    'remotePlugins',
    'disallowedTools',
    'memoryHooks',
    'traceHooks',
    'stderr',
    'onSpend',
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
    maxRetries: 'transientRetries', // the fallback's bound on Claude Code's retries, set per attempt (warmTurn.ts)
    tierOf: 'plugins', // the registry's tiers, widened by each plugin's `toolTiers` (http_request's: `httpTier`)
    builtInPolicy: 'raisedTiers', // Settings → Tools & plugins' raised tiers and disabled built-ins (`disabledTools`)
    live: 'plugins', // reads each call's access as it runs; the sources it reads are the plugins' (its ledger is the slot's turn's)
    approvalGate: 'slot',
    prepareApproval: 'tiers', // bound to the principal, as `mcpServers` is (#4442)
    questionGate: 'slot', // present only for a browser-owned session, which `owner` fingerprints
    systemPromptAppend: 'systemAppend',
    resume: 'spawn', // resume or new id: the transcript tail rule
    sessionId: 'spawn', // as `resume` (transcript tail rule)
    model: 'model',
    effort: 'effort', // a Workflow agent's chat (background/workflowAgents.ts)
    agent: 'agentType', // its subagent definition, by name: the plugin's agents/<name>.md, fixed per image
    mcpServers: 'tiers', // the in-process servers depend on the principal's tiers (and `owner`, `httpRequest`)
    pluginPaths: 'packageHashes',
    allowedPluginPaths: 'allowedPackageHashes',
    builtinTools: 'builtinTools',
    ownPlugin: 'ownPlugin',
    remotePlugins: 'plugins',
    disallowedTools: 'disabledTools', // a local package server's tools set off (#4757)
    browser: 'plugins', // judges the remote plugins' browser_* calls; reads the lists and approvals per call
    memoryHooks: 'slot',
    traceHooks: 'slot',
    stderr: 'spawn', // a constant sink (fingerprint rule)
    onSpend: 'slot', // the credential gateway's meter, to the slot's turn (gateway/server.ts)
  }

  private readonly deps: TurnRunnerDeps

  constructor(deps: TurnRunnerDeps) {
    this.deps = deps
  }

  /** `options.parent` is the turn's span: each step before Claude Code starts is a span under it (#2375). */
  async assemble(
    session: ClaimedSession,
    principal: TurnPrincipal,
    options: { includeCooling?: boolean; parent?: Context } = {},
  ): Promise<AssembledTurn> {
    const id = session.id
    const slot = createTurnSlot()
    const parent = options.parent
    // A turn that sat for half a minute before its first one showed nothing of where it went.
    const phase = <T,>(name: string, run: () => Promise<T>) =>
      withSpan(`agent.turn.${name}`, parent ? { parent } : {}, run)
    const registryTierOf = this.deps.tierOf ?? (() => undefined)
    // Without one, every source and item is at its default.
    const access = this.deps.access ?? DEFAULT_ACCESS
    // The http tools' tier depends on the method in their input (#827), unless
    // http_request's access in ai_access is a tier (#2171), read with its switch below.
    let httpTier: TierResolver = httpTierOf
    const tierOf: TierResolver = (name, input) => httpTier(name, input) ?? registryTierOf(name, input)
    // Widened with the plugins' tiers once they are loaded below, so the
    // panel shows a plugin tool at the tier the permission seam applies.
    // Settings → Tools & plugins' raised tiers (#1953), once read below, over every tier shown.
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
      const candidates = await phase('credentials', () =>
        this.deps.credentials.candidates(options.includeCooling ? { includeCooling: true } : undefined),
      )
      secrets = candidates.map((c) => c.credential.secret)
      const builtInTools = this.deps.builtInTools
      builtIn = builtInTools ? await phase('built_in_tools', () => builtInTools()) : undefined
      const remote = this.deps.remotePlugins
      forwarded = remote ? await phase('remote_plugins', () => remote()) : undefined
      const remotePlugins = [...(forwarded?.plugins ?? [])]
      // Plugin header values (and their bare tokens) are redacted from the
      // event log like the credential. Claude Code never holds them (the
      // forwarder adds them), but a plugin could echo one in a tool result.
      secrets.push(...(forwarded?.secrets ?? []))
      const hindsight = forwarded?.hindsight
      // Sets the memory hooks up only: the recall and retain they run later are
      // inside the run, not under this span.
      const memory = hindsight ? await phase('memory', () => this.memoryHooks(hindsight, secrets, session)) : undefined
      // Settings → plugin packages may switch it off: its source in ai_access (plugins/packages/builtins.ts).
      let ownPlugin =
        this.deps.ownPlugin !== undefined && (await access.enabled(BUILTIN_SOURCES.ownPlugin))
          ? this.deps.ownPlugin
          : undefined
      const packagePlugins = this.deps.packagePlugins
      packages = packagePlugins ? await phase('packages', () => packagePlugins()) : undefined
      const loadedPackages = packages?.loaded ?? []
      // What is switched off in each package and the own plugin, and each package server's
      // switch, in one read each (#4757): what the copies below leave out.
      // No read at all for a turn with neither.
      const itemSources = [...loadedPackages.map((p) => packageSource(p.name)), ...(ownPlugin !== undefined ? [BUILTIN_SOURCES.ownPlugin] : [])]
      const itemAccess = this.deps.access && itemSources.length ? await this.deps.access.packageAccess(itemSources) : new Map<string, PackageAccess>()
      const switches = loadedPackages.length ? await serverAccess(loadedPackages, this.deps.access) : new Map<string, ServerAccess>()
      const colliding = collidingServerNames(loadedPackages)
      const unoffered = unofferedLocalServers(loadedPackages)
      const localRoot = path.join(this.deps.paths.stateDir, 'plugins-local')
      /** The built-ins the allowed packages name that no turn offers: `off` across them. */
      const builtinsOff = builtinsOffOf(loadedPackages, itemAccess)
      // A local server no turn can offer is left out of its copy, and said, as a durable turn says it.
      const problems: string[] = unoffered.map(({ pkg, server }) => unofferableNameProblem(pkg, server))
      /** The own plugin's items when its copy was made (read from its source); else read below. */
      let ownItems: PackageItems | undefined
      // The own plugin as a copy without what is off; with nothing off, as it is. One that
      // cannot be made is not loaded, and said: never loaded with an off item in it.
      if (ownPlugin !== undefined) {
        const omitted = omittedOf(itemAccess.get(BUILTIN_SOURCES.ownPlugin), new Map())
        if (omitsAny(omitted)) {
          try {
            const copy = await derivedPackage(ownPlugin, undefined, localRoot, omitted, OWN_PLUGIN_NAME)
            ownPlugin = copy.dir
            ownItems = copy.items
          } catch (err) {
            problems.push(`ScadBuddy's own plugin was not loaded: ${(err as Error).message}`)
            ownPlugin = undefined
          }
        }
      }
      // Each loaded directory's package and the hash of what Claude Code is handed, by its path.
      const packageNames = new Map<string, string>()
      const loadedHashes = new Map<string, string>()
      /** What each loaded pin holds (derived.ts `derivedPackage`), for the call rule. */
      const loadedItems = new Map<string, PackageItems>()
      if (packages) {
        // Loaded as their derived copies: without their own http MCP servers, which come
        // through the forwarder below (a query with strictMcpConfig off would start them
        // itself, run.ts), and without what is switched off, the local servers whose names
        // another package's server carries among it.
        const loaded = packages
        problems.push(...loaded.problems)
        // A package that cannot be copied is left out and said, as one that cannot be loaded.
        const derived = async (dir: string): Promise<string | undefined> => {
          const pkg = loaded.loaded.find((p) => p.dir === dir)
          if (!pkg) return dir
          try {
            const copy = await derivedPackage(dir, pkg.contentHash, localRoot, packageOmitted(pkg.name, itemAccess, switches, colliding, unoffered), pkg.name)
            packageNames.set(copy.dir, pkg.name)
            loadedHashes.set(pkg.dir, copy.contentHash ?? pkg.contentHash)
            loadedItems.set(pkg.dir, copy.items)
            return copy.dir
          } catch (err) {
            problems.push(`plugin package ${pkg.name} was not loaded: ${(err as Error).message}`)
            return undefined
          }
        }
        const kept = async (dirs: readonly string[]) => (await Promise.all(dirs.map(derived))).filter((d): d is string => d !== undefined)
        packages = {
          ...loaded,
          paths: await kept(loaded.paths),
          allowedPaths: await kept(loaded.allowedPaths),
          builtinTools: loaded.builtinTools.filter((n) => !builtinsOff.has(n)),
          problems,
        }
      }
      // The local servers Claude Code starts for the allowed packages (#4757): each tool set
      // off is removed from the model's context, and each call reads its access (live below).
      const inTurn = loadedPackages.filter((p) => loadedHashes.has(p.dir))
      const allowedLoaded = inTurn.filter((p) => p.allowed)
      const local = localServers(loadedPackages).filter(
        ({ pkg, server }) => allowedLoaded.some((p) => p.name === pkg) && switches.get(packageServerSource(pkg, server))?.enabled !== false,
      )
      const localOff = local.flatMap(({ name, pkg, server }) =>
        (switches.get(packageServerSource(pkg, server))?.disabledTools ?? []).map((tool) => `${toolPrefix(name)}${harnessToolName(tool)}`),
      )
      // The plugins a Skill, Agent or Task call may name, their items from the loaded pins
      // (never a stored review's), so each call is refused when it names one that is off.
      const callPlugins: CallPlugin[] = [
        ...inTurn.map((p) => callPluginOf(p.name, packageSource(p.name), loadedItems.get(p.dir)!)),
        ...(ownPlugin !== undefined ? [callPluginOf(OWN_PLUGIN_NAME, BUILTIN_SOURCES.ownPlugin, ownItems ?? itemsOfPlugin(this.deps.ownPlugin!, OWN_PLUGIN_NAME))] : []),
      ]
      const callBuiltins = [...new Set(allowedLoaded.flatMap((p) => p.builtinTools))].map((name) => ({
        name,
        packages: allowedLoaded.filter((p) => p.builtinTools.includes(name)).map((p) => p.name),
      }))
      // Every package's http MCP servers, through the forwarder like a remote
      // plugin's (#2320); Claude Code loads the packages without them (above).
      packageServers =
        packages && this.deps.packageServers
          ? await this.deps.packageServers(packages.loaded)
          : undefined
      remotePlugins.push(...(packageServers?.plugins ?? []))
      secrets.push(...(packageServers?.secrets ?? []))
      // Each call's access as it is now (#2171, spec §4.4), read by the seam over the turn's
      // tiers; the tier it applies goes in the current turn's ledger, for the server's #2100 check.
      const ledger = turnLedger(slot)
      const live = this.deps.access
        ? {
            access: liveAccess(this.deps.access, {
              tools: this.deps.tools ?? [],
              remote: (forwarded?.plugins ?? []).map((p) => p.name),
              packageServers: [...(packageServers?.servers ?? []), ...local],
              plugins: callPlugins,
              builtins: callBuiltins,
            }),
            ledger,
          }
        : undefined
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
      for (const problem of [...problems, ...(packageServers?.problems ?? [])]) {
        this.deps.stderr?.(`${problem}\n`)
        await unavailable(problem)
      }
      const pluginPaths = [...(this.deps.pluginPaths ?? []), ...(packages?.paths ?? [])]
      const allowedPluginPaths = packages?.allowedPaths ?? []
      const forRun = packages
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
        for (const dir of [...(forRun?.paths ?? []), ...allowedPluginPaths]) {
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
      const [cwd, resume, settingModel, httpAccess] = await phase('session', () =>
        Promise.all([
          ensureSessionDir(this.deps.paths, id),
          this.deps.store.exists(id),
          this.deps.settings?.get<string>(SETTING_MODEL),
          this.deps.httpRequest ? access.entry(BUILTIN_SOURCES.http, HTTP_TOOL) : undefined,
        ]),
      )
      // A Workflow agent's chat (startChild): its own model, effort and subagent definition.
      const model = session.agentModel ?? settingModel
      // Not while that subagent, or the own plugin, is switched off (#4757 Task 6 review I3): the
      // chat's next turn runs as no subagent rather than as one Settings turned off.
      const ownCall = callPlugins.find((p) => p.source === BUILTIN_SOURCES.ownPlugin)
      const ownOff: AccessRow[] = [...(itemAccess.get(BUILTIN_SOURCES.ownPlugin)?.off.subagents ?? [])].map((n) => ({
        source: BUILTIN_SOURCES.ownPlugin,
        item: subagentItem(n),
        access: 'off',
        updated_by: '',
      }))
      const typeOff =
        session.agentType != null &&
        this.deps.ownPlugin !== undefined &&
        (ownCall === undefined || offCallItem('Agent', { subagent_type: session.agentType }, [ownCall], ownOff) !== undefined)
      const agentDefinition = session.agentType && !typeOff ? (await loadAgentDefinitions())[session.agentType] : undefined
      // The http_request tool (#827): on unless `builtin:http` is switched off, or its
      // item is `off`, in ai_access (#2171); an item set to a tier is its tier. Its
      // saved bodies live in Postgres, so any replica serves them (#2374), and it
      // never sees the turn's secrets except to refuse a request that carries one.
      if (httpAccess) httpTier = httpAccessTierOf(httpAccess.access)
      const http =
        this.deps.httpRequest && httpAccess?.enabled && httpAccess.access !== 'off'
          ? httpRequestServer(
              slotHttpContext(
                {
                  saved: new PgHttpBodies(this.deps.sql, id),
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
      // The built-ins the packages name that the session serves instead of Claude Code: the
      // background ones, kept past the turn, and WebFetch, pinned to the addresses it checked (#2684).
      const served = allowedPluginPaths.length
        ? (packages?.builtinTools ?? []).filter(
            (n) => CLASSIC_BUILTINS.includes(n) && (this.deps.background !== undefined || !BACKGROUND_BUILTINS.includes(n)),
          )
        : []
      const run: AssembledTurn['run'] = {
        paths: this.deps.paths,
        cwd,
        sessionStore: this.deps.store,
        includePartialMessages: true,
        maxTurns: session.maxTurns,
        maxBudgetUsd: Math.max(session.budgetUsd - session.costUsd, 0.000001),
        tierOf,
        // The headless browser's lists (#982): every remote plugin's browser_* calls.
        browser: browserGuard({
          store: new BrowserOrigins(this.deps.sql, this.deps.settings),
          sessionId: id,
          plugins: remotePlugins.map((p) => p.name),
        }),
        ...(builtIn ? { builtInPolicy: builtIn } : {}),
        ...(live ? { live } : {}),
        approvalGate: (request) => {
          const turn = slot.current
          return turn ? turn.approvalGate(request) : Promise.resolve({ approved: false, message: NO_TURN })
        },
        // A registry tool that resolves what its arguments name parks that, by name (#4442).
        ...(this.deps.prepareApproval ? { prepareApproval: this.deps.prepareApproval(session, principal) } : {}),
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
        systemPromptAppend: served.length ? `${systemAppend()}\n\n${classicBuiltinNote(served)}` : systemAppend(),
        // First turn: the SDK session gets OUR id; later turns resume it.
        ...(resume ? { resume: id } : { sessionId: id }),
        ...(typeof model === 'string' && model ? { model } : {}),
        ...(session.agentEffort ? { effort: session.agentEffort } : {}),
        // The plugin's subagent of that name (its agents/<name>.md), as the turn's main thread.
        ...(agentDefinition && session.agentType ? { agent: { name: session.agentType, definition: agentDefinition } } : {}),
        ...(this.deps.mcpServers || http || served.length
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
                      ...(live ? { ledger } : {}),
                      // The turn's credentials and plugin tokens, which workspace_read redacts (#2329).
                      secrets: () => secrets,
                    })
                  : {}),
                ...(http ? { [HTTP_SERVER]: http } : {}),
                ...(served.length
                  ? {
                      [BUILTIN_SERVER]: classicBuiltinServer(served, {
                        ...(this.deps.background
                          ? {
                              background: {
                                tasks: this.deps.background.tasks,
                                sessionId: id,
                                owner: session.owner,
                                tz: this.deps.background.tz,
                                now: () => new Date(),
                                // A Monitor's command runs where this turn's Bash runs: this replica.
                                commandQueue: this.deps.background.commandQueue,
                                workspace: this.deps.files,
                              },
                            }
                          : {}),
                        web: slotWebContext(
                          {
                            // The answer's model call, on the credential the turn is on now (#2973);
                            // its cost is the turn's. None (an empty pool, or no turn) fails closed.
                            nested: (this.deps.nestedQueries ?? nestedQueries)({
                              paths: this.deps.paths,
                              credential: async () => {
                                const on = slot.current?.credential?.current
                                if (!on) throw new NoUsableCredentialError('no Claude credential is usable', undefined)
                                return on.credential
                              },
                              spent: async (cost) => {
                                if (slot.current?.nested) slot.current.nested.usd += cost
                              },
                              // The row's cost moves at the turn's end, so what this turn's own queries spent comes off too.
                              remaining: async () => {
                                const left = await poolRemainingUsd(this.deps.sql, id)
                                return left === undefined ? undefined : left - (slot.current?.nested?.usd ?? 0)
                              },
                            }),
                            secrets: () => secrets,
                            audit: this.deps.audit,
                            actor: session.owner,
                            sessionId: id,
                            ...(this.deps.httpRequest?.resolve ? { resolve: this.deps.httpRequest.resolve } : {}),
                          },
                          slot,
                        ),
                      }),
                    }
                  : {}),
              },
            }
          : {}),
        ...(pluginPaths.length ? { pluginPaths } : {}),
        ...(allowedPluginPaths.length
          ? { allowedPluginPaths, builtinTools: (packages?.builtinTools ?? []).filter((n) => !served.includes(n)) }
          : {}),
        ...(ownPlugin !== undefined ? { ownPlugin } : {}),
        ...(remotePlugins.length ? { remotePlugins } : {}),
        disallowedTools: localOff,
        // Shaped like any turn's memory hooks (the same config gives the same
        // events, matchers and timeouts); each runs the current turn's own.
        ...(memory ? { memoryHooks: slotHooks(memory.hooks('', '').hooks, slot, (turn) => turn.memory?.hooks) } : {}),
        traceHooks: TurnTrace.hooksFor(() => slot.current?.trace),
        ...(this.deps.stderr ? { stderr: this.deps.stderr } : {}),
        // What the credential gateway priced, to the turn the process serves (turnCore.ts `runningUsd`).
        onSpend: (usd) => slot.meter(usd),
      }
      inProcessServers = Object.keys(run.mcpServers ?? {})
      const first = candidates[0]
      const fingerprintParts: FingerprintParts = {
        credentialId: first?.id ?? '',
        // The row's epoch moves with every new secret and reset (credentials.ts).
        credentialUpdatedAt: first ? String(first.epoch) : '',
        model: run.model ?? null,
        effort: run.effort ?? null,
        agentType: run.agent?.name ?? null,
        // What was loaded: the own plugin's path names its copy's hash when something in it is off.
        ownPlugin: ownPlugin ?? null,
        // Each plugin's configured endpoint and header version, never the loopback
        // URL this assembly registered (forwarder.ts `pluginEndpoint`).
        plugins: [...(forwarded?.endpoints ?? []), ...(packageServers?.endpoints ?? [])],
        // What Claude Code is handed: each derived copy's hash, which moves with what is off in it.
        packageHashes: loadedPackages.map((p) => loadedHashes.get(p.dir) ?? p.contentHash),
        allowedPackageHashes: loadedPackages.filter((p) => p.allowed).map((p) => loadedHashes.get(p.dir) ?? p.contentHash),
        tiers: principal.tiers ? [...principal.tiers] : null,
        owner: `${session.owner.kind}:${session.owner.id}`,
        systemAppend: run.systemPromptAppend ?? '',
        // Settings → Tools & plugins' disabled built-ins; each plugin's own are in its `plugins` entry.
        // And each local package server's tools set off (#4757).
        disabledTools: [...(builtIn?.disallowed ?? []), ...localOff],
        raisedTiers: [...(builtIn?.raised ?? [])],
        httpRequest: http !== undefined,
        httpTier: httpAccess?.access ?? null,
        builtinTools: run.builtinTools ?? [],
        servedBuiltins: served,
        backgroundTasks: this.deps.background !== undefined,
        recallMaxQueryChars: memory?.recallMaxQueryChars ?? null,
        transientRetries: null,
      }
      return {
        run,
        candidates,
        fingerprintParts,
        slot,
        secrets,
        eventTierOf: (name, input) => eventTierOf(name, input),
        shownTierOf,
        observe: pluginCheck,
        ...(memory ? { memory: memory.hooks } : {}),
        terminate: async () => {
          await Promise.all([forwarded?.terminate(), packageServers?.terminate()])
        },
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

/**
 * The seam's ledger (harness/seamLedger.ts) as the assembly's callbacks see it: a fresh
 * one for each turn the slot holds, dropped with it, so a warm process that serves many
 * turns never holds more than one turn's calls. With no turn it records nothing.
 */
function turnLedger(slot: TurnSlot): Pick<SeamLedger, 'record' | 'tierOf'> {
  const ledgers = new WeakMap<TurnContext, SeamLedger>()
  return {
    record(toolUseId, tier) {
      const turn = slot.current
      if (!turn) return
      let ledger = ledgers.get(turn)
      if (!ledger) ledgers.set(turn, (ledger = new SeamLedger()))
      ledger.record(toolUseId, tier)
    },
    tierOf(toolUseId) {
      const turn = slot.current
      return turn ? ledgers.get(turn)?.tierOf(toolUseId) : undefined
    },
  }
}
