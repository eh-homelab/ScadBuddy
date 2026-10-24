import { mkdir, readdir, rm, stat } from 'node:fs/promises'
import path from 'node:path'
import type { Sql } from 'postgres'
import type { AuditSink } from '../audit/log.js'
import { BACKGROUND_BUILTINS, BUILTIN_SERVER, builtinServer, PROVIDED } from '../builtins/server.js'
import type { BackgroundTasks } from '../background/service.js'
import type { NestedQueries } from '../builtins/web.js'
import { dirBodies, HTTP_READ_TOOL_NAME, HTTP_SERVER, httpRequestEnabled, httpRequestServer, httpTierOf, type SavedBodies, SETTING_HTTP_REQUEST } from '../harness/httpRequest.js'
import { PgHttpBodies } from '../sessions/httpBodies.js'
import type { HttpLimits } from '../harness/httpRequest.js'
import type { Resolver } from '../http/egress.js'
import type { PluginsForRun } from '../plugins/forwarder.js'
import type { LoadedPackage, PackagesForRun } from '../plugins/packages/install.js'
import type { PackageServers } from '../plugins/packages/mcp.js'
import { pluginTierResolver } from '../plugins/registry.js'
import { browserGuard, type BrowserOriginsAccess } from '../browser/guard.js'
import type { SessionFiles } from '../workspace/store.js'
import { localPackageServers, sharePackage, unparkableBuiltins } from './turnPackages.js'
import { turnToolName } from './turnTools.js'
import {
  connectHttp,
  connectInProcess,
  type TurnContext,
  type TurnPackage,
  type TurnServer,
  type TurnServers,
} from './turnTools.js'

// What a durable turn's other tools are built from (#2320): the same pieces a classic
// turn's `runTurn` hands Claude Code (sessions/manager.ts), built again for each listing
// and each call, so a plugin disabled, removed or failing its egress check since is gone
// at once, as it would be from a classic turn's next query.

/**
 * Under the state dir: a flow run's saved http bodies, `flow-<id>/http` (#2496). A durable
 * session's bodies are in Postgres since #2374; a bare `<id>` directory is one a pod left
 * before that, cleared by the sweep and by forget.
 */
export const DURABLE_TURNS_DIR = 'durable-turns'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
/** How long a flow run's saved bodies are kept: its run has no row here to outlive. */
export const FLOW_BODIES_KEPT_MS = 7 * 24 * 3600_000

/**
 * Removes one subject's saved bodies on this pod (`session-<id>` or `flow-<id>`, forget.ts).
 * A session's is only a directory left by a pod that ran before #2374.
 */
export async function removeTurnDir(root: string, subject: string): Promise<void> {
  const name = subject.startsWith('session-') ? subject.slice('session-'.length) : subject
  if (!UUID.test(name) && !(name.startsWith('flow-') && UUID.test(name.slice('flow-'.length)))) return
  await rm(path.join(root, name), { recursive: true, force: true })
}

/**
 * The saved bodies of flow runs older than FLOW_BODIES_KEPT_MS on this pod (#2496), and
 * the directories of durable sessions that no longer exist, which only a pod that ran
 * before #2374 wrote: each replica keeps its own (the server is pinned), so each sweeps
 * its own, whichever pod the session was forgotten on.
 */
export async function sweepTurnDirs(root: string, sql: Sql, now = Date.now()): Promise<string[]> {
  const names = await readdir(root).catch((err: NodeJS.ErrnoException) => {
    if (err.code === 'ENOENT') return [] as string[]
    throw err
  })
  const sessions = names.filter((n) => UUID.test(n))
  const live = new Set(
    sessions.length ? (await sql<{ id: string }[]>`SELECT id::text FROM ai_sessions WHERE id = ANY(${sessions}::uuid[])`).map((r) => r.id) : [],
  )
  const removed: string[] = []
  for (const name of names) {
    const dir = path.join(root, name)
    let stale = false
    if (UUID.test(name)) stale = !live.has(name)
    else if (name.startsWith('flow-') && UUID.test(name.slice('flow-'.length))) {
      stale = now - (await stat(dir)).mtimeMs >= FLOW_BODIES_KEPT_MS
    }
    if (!stale) continue
    await rm(dir, { recursive: true, force: true })
    removed.push(name)
  }
  return removed
}

export type TurnServerDeps = {
  /** The enabled remote plugins through the loopback forwarder (main.ts `remotePlugins`). */
  remotePlugins?: (() => Promise<PluginsForRun>) | undefined
  settings?: { get<T>(key: string): Promise<T | undefined> } | undefined
  /**
   * The headless browser's lists and the sessions' approved origins (#982,
   * browser/store.ts): the remote plugins' browser_* calls are judged by them, as a
   * classic turn's are (browser/guard.ts). Without it they run at their tiers.
   */
  browserOrigins?: BrowserOriginsAccess | undefined
  /** Every usable credential's secret (fallback.ts candidates): never in a request, never in a result. */
  credentialSecrets?: (() => Promise<readonly string[]>) | undefined
  /**
   * http_request, as a classic turn has it. A session's saved bodies are in Postgres
   * (`sql`, #2374), as a classic turn's are; a flow's, which has no session, under
   * `saveRoot/flow-<run>/http` on this pod's own disk (never the shared payload volume).
   */
  httpRequest?:
    | {
        saveRoot: string
        sql: Sql
        audit?: AuditSink | undefined
        resolve?: Resolver
        limits?: Partial<HttpLimits>
        /** The sessions' workspaces, where `save_to_workspace` stores a body (#2496), as a classic turn's. */
        files?: SessionFiles | undefined
      }
    | undefined
  /**
   * The enabled plugin packages (`loadPackagesForRun`, as a classic turn's), shared with
   * the segment runner under `packages.root` (turnPackages.ts) on a listing, and their
   * MCP servers offered as turn tools.
   */
  packages?:
    | {
        load: () => Promise<PackagesForRun>
        root: string | undefined
        /** Where a package's copy without its http MCP servers is made, on this pod (as a classic turn's). */
        localRoot: string
        /** Their http MCP servers through the loopback forwarder (plugins/packages/mcp.ts). */
        forward: (loaded: readonly LoadedPackage[]) => Promise<PackageServers>
      }
    | undefined
  /**
   * The Claude Code built-ins an allowed package names, beyond what the engine runs itself
   * (#2393, builtins/server.ts): offered as the `builtin` turn tools, on the session's
   * workspace and its credential. Without it they are not offered, and the session says so.
   */
  builtins?:
    | {
        files: SessionFiles
        /** The model work WebFetch and WebSearch need, for the session the call is in. */
        nested: (context: TurnContext) => NestedQueries
        audit?: AuditSink | undefined
        resolve?: Resolver
        /** The session's background tasks (CronCreate, ScheduleWakeup, TaskStop); without them those are not offered. */
        background?: { tasks: BackgroundTasks; tz: string; now?: () => Date; commandQueue?: string } | undefined
      }
    | undefined
}

/** What the model is told about the built-ins it gets as turn tools. */
export function builtinNote(names: readonly string[]): string {
  const tools = names.map((n) => `${n} (mcp__durable__${turnToolName(BUILTIN_SERVER, n)})`).join(', ')
  return (
    `Claude Code's built-in tools ${tools} are provided in this session as tools of the "${BUILTIN_SERVER}" server, because a ` +
    'plugin package you have names them. Use them where its skills or agents say to use the built-in of that name: they take ' +
    'the same input and answer the same way. Every call of one asks the user first. Their files are the session workspace, ' +
    'shown under /workspace (the workspace_* tools see the same files), not a local disk.' +
    (names.some((n) => BACKGROUND_BUILTINS.includes(n))
      ? ' The session keeps the jobs, wakeups, watches and workflows they start past this turn: each sends its prompt, events or result to the ' +
        'session as a new message when it is due and the session is idle.'
      : '')
  )
}

export function turnServers(deps: TurnServerDeps): (context: TurnContext) => Promise<TurnServers> {
  return async (context) => {
    const servers: TurnServer[] = []
    const forwarded = deps.remotePlugins ? await deps.remotePlugins() : undefined
    // The forwarder URLs carry the registration's token: as secret as the header it adds.
    const secrets: string[] = [
      ...(forwarded?.secrets ?? []),
      ...(forwarded?.plugins ?? []).map((p) => p.url),
      ...(deps.credentialSecrets ? await deps.credentialSecrets() : []),
    ]
    // A flow run has no session to remember an approval in.
    const browserPlugins = (forwarded?.plugins ?? []).map((p) => p.name)
    const browser = deps.browserOrigins
      ? browserGuard({ store: deps.browserOrigins, sessionId: context.flow ? undefined : context.session, plugins: browserPlugins })
      : undefined
    const browserParts = browser
      ? {
          guard: (name: string, input: unknown) => browser.check(name, input),
          screen: (name: string, input: unknown, text: string) => browser.screen(name, input, text),
        }
      : {}
    for (const plugin of forwarded?.plugins ?? []) {
      const tiers = pluginTierResolver([plugin])
      // An unlisted plugin tool is outward (registry.ts), as in a classic turn. One
      // function for the label, the park and the call, so they cannot disagree (#2838).
      const tierOf = (name: string) => tiers(name) ?? 'outward'
      const keep = forwarded?.keep?.(plugin.name)
      servers.push({
        name: plugin.name,
        connect: () => connectHttp(plugin.url),
        // Its forwarder URL is new each run; its tools change with what Settings disabled.
        listingKey: JSON.stringify(plugin.disabledTools),
        tierOf,
        // A browser call's tier depends on the page it opens (#982): any may park.
        mayPark: (name) => browser?.covers(name) === true || tierOf(name) === 'outward',
        fixedTier: (name) => (browser?.covers(name) && name.endsWith('__browser_navigate') ? undefined : tierOf(name)),
        ...browserParts,
        // Its MCP session is kept for the durable session, on its own route (#2581).
        ...(keep
          ? {
              keep: {
                fingerprint: keep.fingerprint,
                connect: async () => {
                  const route = keep.register()
                  try {
                    return { client: await connectHttp(route.url), release: route.release }
                  } catch (err) {
                    route.release()
                    throw err
                  }
                },
              },
            }
          : {}),
      })
    }
    const http = deps.httpRequest
    if (http && httpRequestEnabled(await deps.settings?.get<unknown>(SETTING_HTTP_REQUEST))) {
      let saved: SavedBodies
      if (context.flow) {
        const saveDir = path.join(http.saveRoot, context.session, 'http')
        await mkdir(saveDir, { recursive: true })
        saved = dirBodies(saveDir)
      } else saved = new PgHttpBodies(http.sql, context.session)
      servers.push({
        name: HTTP_SERVER,
        connect: () =>
          connectInProcess(
            () =>
              httpRequestServer({
                saved,
                secrets: () => secrets,
                audit: http.audit,
                actor: context.owner,
                // A flow's call has no session to audit it under (its saved bodies are under `flow-<run>`).
                sessionId: context.flow ? undefined : context.session,
                ...(context.turnId ? { turnId: context.turnId } : {}),
                // A flow's workspace is its starter's own, the one its `tool('workspace_read', …)` reads
                // (workspaceOf, a call in no session), so a binary body it fetched is readable (#2511).
                ...(http.files
                  ? {
                      workspace: {
                        files: http.files,
                        ref: context.flow
                          ? { kind: 'principal' as const, principalKind: context.owner.kind, principalId: context.owner.id }
                          : { kind: 'session' as const, sessionId: context.session },
                      },
                    }
                  : {}),
                signal: context.signal,
                ...(http.resolve ? { resolve: http.resolve } : {}),
                ...(http.limits ? { limits: http.limits } : {}),
              }).instance,
          ),
        tierOf: (name, input) => httpTierOf(name, input) ?? 'outward',
        // http_request is outward by its method; only reading a saved body never is.
        mayPark: (name) => name !== HTTP_READ_TOOL_NAME,
        // http_request's tier is its method's; reading a saved body is always read.
        fixedTier: (name) => (name === HTTP_READ_TOOL_NAME ? 'read' : undefined),
        pinned: true,
      })
    }
    const problems = [...(forwarded?.problems ?? [])]
    const notes: string[] = []
    // A flow has no session workspace for the file tools to act on, nor a turn to tell.
    const provided: ReadonlySet<string> =
      deps.builtins && !context.flow
        ? new Set([...PROVIDED].filter((n) => deps.builtins!.background || !BACKGROUND_BUILTINS.includes(n)))
        : new Set()
    const loaded = deps.packages ? await deps.packages.load() : undefined
    let forwardedPackages: PackageServers | undefined
    let shared: TurnPackage[] | undefined
    if (loaded) {
      problems.push(...loaded.problems)
      // A listing shares each package first: one that fails to share (or to be stripped
      // of its http servers) is not loaded, so its servers are not offered either (#2563).
      let offered = loaded.loaded
      // One binding for both branches: the listing and a call decide on the same root (#3523).
      const { root, localRoot } = deps.packages!
      if (context.listing) {
        shared = []
        const failed = new Set<string>()
        for (const pkg of loaded.loaded) {
          if (root === undefined) {
            problems.push(
              `plugin package ${pkg.name} was not loaded: a durable session reads packages from the payload volume, and SCADBUDDY_PAYLOAD_DIR is not set`,
            )
            failed.add(pkg.name)
            continue
          }
          try {
            const turnPackage = await sharePackage(pkg, root, localRoot)
            const dark = unparkableBuiltins(turnPackage, provided)
            if (dark.length) {
              problems.push(
                `plugin package ${pkg.name} names ${dark.join(', ')}: a durable session cannot ask you before ` +
                  'such a call runs, so they are not offered here; a classic session offers them',
              )
            }
            // The ones provided as turn tools are not the engine's to run.
            shared.push({ ...turnPackage, builtin_tools: turnPackage.builtin_tools.filter((t) => !dark.includes(t) && !provided.has(t)) })
          } catch (err) {
            problems.push(`plugin package ${pkg.name} was not loaded: ${(err as Error).message}`)
            failed.add(pkg.name)
          }
        }
        offered = loaded.loaded.filter((pkg) => !failed.has(pkg.name))
      } else if (root === undefined) {
        // With no payload volume a listing shares nothing, so a call builds nothing
        // either, whatever it names (#3072).
        offered = []
      }
      // Otherwise a call (not a listing) builds every loaded package's servers: a share
      // that threw is known only by sharing again, so for that one the listing is the
      // guard, since a call can only name a tool the listing offered (#2620).
      servers.push(...localPackageServers(offered, problems))
      const named = [...new Set(offered.filter((p) => p.allowed).flatMap((p) => p.builtinTools))].filter((n) => provided.has(n)).sort()
      const builtins = deps.builtins
      if (named.length && builtins) {
        const files = { files: builtins.files, ws: { kind: 'session' as const, sessionId: context.session } }
        const web = {
          nested: builtins.nested(context),
          secrets: () => secrets,
          audit: builtins.audit,
          actor: context.owner,
          sessionId: context.session,
          ...(context.turnId ? { turnId: context.turnId } : {}),
          signal: context.signal,
          ...(builtins.resolve ? { resolve: builtins.resolve } : {}),
        }
        const tasks = builtins.background
        const background = tasks
          ? {
              tasks: tasks.tasks,
              sessionId: context.session,
              owner: context.owner,
              tz: tasks.tz,
              now: tasks.now ?? (() => new Date()),
              commandQueue: tasks.commandQueue,
              workspace: builtins.files,
            }
          : undefined
        servers.push({
          name: BUILTIN_SERVER,
          connect: () => connectInProcess(() => builtinServer(named, () => ({ files, web, background }))),
          // Every package built-in is outward, as in a classic turn (run.ts `builtinTools`).
          tierOf: () => 'outward',
          mayPark: () => true,
          callIds: true,
        })
        notes.push(builtinNote(named))
      }
      // As a remote plugin's: the forwarder URL, which carries its token, is a secret too.
      forwardedPackages = await deps.packages!.forward(offered)
      problems.push(...forwardedPackages.problems)
      secrets.push(...forwardedPackages.secrets)
      for (const plugin of forwardedPackages.plugins) {
        browserPlugins.push(plugin.name)
        servers.push({ name: plugin.name, connect: () => connectHttp(plugin.url), tierOf: () => 'outward', ...browserParts })
      }
    }
    return {
      servers,
      problems,
      secrets,
      ...(shared ? { packages: shared } : {}),
      ...(notes.length ? { notes } : {}),
      release: () => {
        forwardedPackages?.release()
        loaded?.release()
        return forwarded?.release()
      },
    }
  }
}
