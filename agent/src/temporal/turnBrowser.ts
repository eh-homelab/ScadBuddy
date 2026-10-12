import { readFileSync } from 'node:fs'
import path from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import type { Sql } from 'postgres'
import {
  describeDrained,
  DOWNLOAD_WAIT_MS,
  type DownloadTarget,
  drainDownloads,
  MAX_DOWNLOADS_PER_CALL,
  MAX_DOWNLOADS_PER_TURN,
  writeDownloadBudget,
} from '../harness/browserDownloads.js'
import { browserOrigins, loadApprovedOrigins, rememberDurableOrigin } from '../harness/browserOrigins.js'
import type { UiReachMap } from '../harness/browserReach.js'
import {
  assertHeadlessPlugin,
  browserDownloadsDir,
  browserInputGuard,
  browserTierDependsOnInput,
  browserTierOf,
  DISALLOWED_BROWSER_TOOLS,
  GRANT_SERVER,
  type HeadlessBrowserOptions,
  type HeadlessBrowserPlugin,
  materializeHeadlessBrowser,
  NO_DOWNLOADS,
  originToApprove,
  PLUGIN_NAME,
  SERVER_NAME,
  SETTING_HEADLESS_BROWSER,
} from '../harness/headlessBrowser.js'
import { durableGrantServer } from '../harness/headlessGrants.js'
import type { RiskTier } from '../harness/permissions.js'
import type { HarnessPaths } from '../harness/options.js'
import { removeSessionBrowserDirs, sessionBrowserDir, sessionBrowserTmpDir } from '../harness/stateDirs.js'
import { normaliseOrigin } from '../http/origins.js'
import type { BuiltInPolicy } from '../plugins/builtInTools.js'
import { connectInProcess, type TurnContext, type TurnServer } from './turnTools.js'

// The headless browser in a durable session (#2320). A classic turn hands Claude Code
// the vendored playwright plugin, which Claude Code starts for the turn and which dies
// with it (sessions/manager.ts). A durable turn's tools run as activities, one call at a
// time, so the agent starts the same server itself, the same way (materializeHeadlessBrowser,
// assertHeadlessPlugin: `env -i`, the pinned cli.js, --isolated, the session's own
// config and redirect guard), and keeps it for the session's calls on this replica: the
// browser's server is pinned (TurnServer.pinned), so every call of the turn reaches this
// process. It is closed once the session has not used it for BROWSER_IDLE_MS, has no call
// in flight and is between turns; it is restarted only when a setting it was started with
// changes (browserKey), and a turn reads those options once (#2522).
//
// The same rules as a classic turn's permission seam apply to every call: the fixed deny
// list, each tool's tier (BROWSER_TOOL_TIERS; an unlisted one is outward), the filename
// and origin rules (browserInputGuard: a refused origin is refused, one a human may allow
// is outward and remembered once approved), and Settings → Plugins' raised tiers and
// disabled tools for the `playwright` set. The grant tool (`scadbuddy_browser`) is
// outward and writes a grant bound to the call's own approval (recordDurableGrant).

/** A durable session's browser is closed this long after its last call. */
export const BROWSER_IDLE_MS = 10 * 60_000

/** The server name a durable turn's browser tools carry, so their harness names are the classic ones (TOOL_PREFIX). */
export const BROWSER_SERVER = `plugin_${PLUGIN_NAME}_${SERVER_NAME}`

export type HeadlessBrowserDeps = {
  backendUrl: string
  publicUrl?: string
  livePublicUrl?: () => Promise<string | undefined>
  uiOrigins?: string
  browserAllowedOrigins?: string
  executablePath?: string
  sandbox?: () => Promise<boolean>
  probe?: (origins: readonly string[]) => Promise<UiReachMap>
}

export type Hosted = { client: Client; plugin: HeadlessBrowserPlugin; key: string; lastUsed: number; inFlight: number }

/**
 * What a running browser is restarted for: the settings it was started with. The UI
 * probe's answer and the approved origins are left out, so a flaky probe or a newly
 * approved origin never restarts the browser under a turn (#2522).
 */
export function browserKey(options: HeadlessBrowserOptions): string {
  return JSON.stringify({ ...options, uiReach: undefined, approvedOrigins: undefined, rememberOrigin: undefined })
}

/** The browsers this replica runs for durable sessions, one per session. */
export class BrowserHost {
  readonly #paths: HarnessPaths
  readonly #idleMs: number
  readonly #active: ((session: string) => Promise<boolean>) | undefined
  readonly #start: (options: HeadlessBrowserOptions, key: string) => Promise<Hosted>
  readonly #closed: ((session: string) => Promise<void>) | undefined
  readonly #hosted = new Map<string, Hosted>()
  // Each session's starts and closes run one after another, so two calls never start two
  // browsers for one session, nor close one the other is using (#2522).
  readonly #queue = new Map<string, Promise<unknown>>()
  readonly #drained = new Map<Hosted, (() => void)[]>()
  readonly #timer: NodeJS.Timeout

  constructor(options: {
    paths: HarnessPaths
    idleMs?: number
    /** Whether the session's turn is running now: its browser is kept while it is, even parked on an approval. */
    active?: (session: string) => Promise<boolean>
    start?: (options: HeadlessBrowserOptions, key: string) => Promise<Hosted>
    /** Once a session's browser has ended, before its folders go: keeps what it downloaded since its last call (#2402). */
    closed?: (session: string) => Promise<void>
  }) {
    this.#paths = options.paths
    this.#idleMs = options.idleMs ?? BROWSER_IDLE_MS
    this.#active = options.active
    this.#start = options.start ?? startBrowser
    this.#closed = options.closed
    this.#timer = setInterval(() => void this.reap(), Math.min(this.#idleMs, 60_000))
    this.#timer.unref()
  }

  #serial<T>(session: string, run: () => Promise<T>): Promise<T> {
    const next = (this.#queue.get(session) ?? Promise.resolve()).then(run)
    const tail = next.catch(() => {})
    this.#queue.set(session, tail)
    void tail.then(() => {
      if (this.#queue.get(session) === tail) this.#queue.delete(session)
    })
    return next
  }

  /**
   * The session's browser, started with these options, or the running one if its key is
   * the same; held for one use until `release`. One started with other settings is closed
   * once its calls in flight are done, and started again with these.
   */
  browser(session: string, options: HeadlessBrowserOptions): Promise<Hosted> {
    const key = browserKey(options)
    return this.#serial(session, async () => {
      const running = this.#hosted.get(session)
      if (running && running.key !== key) await this.#close(session, running)
      const hosted = this.#hosted.get(session) ?? (await this.#start(options, key))
      this.#hosted.set(session, hosted)
      hosted.inFlight++
      hosted.lastUsed = Date.now()
      return hosted
    })
  }

  /** One use of `browser` is over. */
  release(hosted: Hosted): void {
    hosted.inFlight = Math.max(0, hosted.inFlight - 1)
    hosted.lastUsed = Date.now()
    if (hosted.inFlight === 0) for (const done of this.#drained.get(hosted)?.splice(0) ?? []) done()
  }

  async #close(session: string, hosted: Hosted): Promise<void> {
    if (hosted.inFlight > 0) {
      await new Promise<void>((done) => this.#drained.set(hosted, [...(this.#drained.get(hosted) ?? []), done]))
    }
    this.#drained.delete(hosted)
    if (this.#hosted.get(session) === hosted) this.#hosted.delete(session)
    await hosted.client.close().catch(() => {})
    await this.#closed?.(session).catch(() => {})
    await removeSessionBrowserDirs(this.#paths, session).catch(() => {})
  }

  close(session: string): Promise<void> {
    return this.#serial(session, async () => {
      const hosted = this.#hosted.get(session)
      if (hosted) await this.#close(session, hosted)
    })
  }

  /** Closes each browser idle for `idleMs` with no call in flight, unless its session's turn is still running. */
  async reap(now = Date.now()): Promise<void> {
    const idle = (h: Hosted | undefined): h is Hosted => h !== undefined && h.inFlight === 0 && now - h.lastUsed >= this.#idleMs
    for (const session of [...this.#hosted.keys()]) {
      if (!idle(this.#hosted.get(session))) continue
      // A lookup that fails keeps the browser: the next pass asks again.
      if (this.#active && (await this.#active(session).catch(() => true))) continue
      await this.#serial(session, async () => {
        const hosted = this.#hosted.get(session)
        if (idle(hosted)) await this.#close(session, hosted)
      })
    }
  }

  async stop(): Promise<void> {
    clearInterval(this.#timer)
    await Promise.all([...this.#hosted.keys()].map((s) => this.close(s)))
  }
}

async function startBrowser(options: HeadlessBrowserOptions, key: string): Promise<Hosted> {
  const plugin = materializeHeadlessBrowser(options)
  assertHeadlessPlugin(plugin.pluginDir)
  const config = JSON.parse(readFileSync(path.join(plugin.pluginDir, '.mcp.json'), 'utf8')) as {
    mcpServers: Record<string, { command: string; args: string[] }>
  }
  const server = config.mcpServers[SERVER_NAME]!
  const client = new Client({ name: 'scadbuddy-durable-browser', version: '1' })
  // `env -i` in the command clears what the transport would pass on (serverCommand).
  await client.connect(new StdioClientTransport({ command: server.command, args: server.args, stderr: 'ignore' }))
  return { client, plugin, key, lastUsed: Date.now(), inFlight: 0 }
}

export type TurnBrowserDeps = {
  sql: Sql
  paths: HarnessPaths
  host: Pick<BrowserHost, 'browser' | 'release'>
  headless: HeadlessBrowserDeps
  settings?: { get<T>(key: string): Promise<T | undefined> } | undefined
  builtIn?: (() => Promise<BuiltInPolicy>) | undefined
  /** The workspace the browser's downloads go to (#2402); without it nothing downloads. */
  files?: DownloadTarget['files'] | undefined
}

/** The options a classic turn would start this session's browser with (sessions/manager.ts `runTurn`), but its approved origins. */
async function optionsFor(deps: TurnBrowserDeps, session: string, before: HeadlessBrowserOptions | undefined): Promise<HeadlessBrowserOptions> {
  const hb = deps.headless
  const sandbox = hb.sandbox ? await hb.sandbox() : false
  // A read that fails keeps the URL the session's browser already has.
  const livePublicUrl = hb.livePublicUrl ? await hb.livePublicUrl().catch(() => before?.livePublicUrl) : undefined
  // Each UI origin asked whether it answers without a login, so the browser is sent to one that does.
  let uiReach: UiReachMap | undefined
  if (hb.probe) {
    try {
      const { ui } = browserOrigins({ backendUrl: hb.backendUrl, publicUrl: hb.publicUrl, livePublicUrl, uiOrigins: hb.uiOrigins })
      uiReach = await hb.probe(ui).catch(() => undefined)
    } catch {
      uiReach = undefined
    }
  }
  return {
    ...(uiReach ? { uiReach } : {}),
    ...(sandbox ? { sandbox: true } : {}),
    sessionId: session,
    backendUrl: hb.backendUrl,
    ...(hb.publicUrl ? { publicUrl: hb.publicUrl } : {}),
    ...(livePublicUrl ? { livePublicUrl } : {}),
    ...(hb.uiOrigins ? { uiOrigins: hb.uiOrigins } : {}),
    ...(hb.browserAllowedOrigins ? { browserAllowedOrigins: hb.browserAllowedOrigins } : {}),
    dir: sessionBrowserDir(deps.paths, session),
    tmpDir: sessionBrowserTmpDir(session),
    ...(hb.executablePath ? { executablePath: hb.executablePath } : {}),
  }
}

/** The turn a durable session is running now (`ai_sessions.durable_turn_id`), or null between turns. */
async function turnOf(sql: Sql, session: string): Promise<string | null> {
  const [row] = await sql<{ durable_turn_id: string | null }[]>`SELECT durable_turn_id FROM ai_sessions WHERE id = ${session}`
  return row?.durable_turn_id ?? null
}

const DENIED = new Set<string>(DISALLOWED_BROWSER_TOOLS)
/** How many sessions' options a replica keeps. */
const OPTIONS_KEPT = 256

/** The headless browser's turn servers, when Settings has it on; none otherwise. */
export function turnBrowserServers(deps: TurnBrowserDeps): (context: TurnContext) => Promise<TurnServer[]> {
  // Each session's options as its current turn first read them: a turn's calls share them,
  // as a classic turn's do, so the probe and the settings read run once a turn (#2522).
  const kept = new Map<string, { turn: string | null; options: HeadlessBrowserOptions }>()
  const turnOptions = async (session: string) => {
    const turn = await turnOf(deps.sql, session)
    const was = kept.get(session)
    if (was && was.turn === turn) return was.options
    const options = await optionsFor(deps, session, was?.options)
    kept.delete(session)
    kept.set(session, { turn, options })
    if (kept.size > OPTIONS_KEPT) kept.delete(kept.keys().next().value!)
    return options
  }
  // Each session's download budget for its current turn, as run.ts's hooks keep one for a
  // classic turn: the call count, and the room the workspace had at the turn's first call.
  const budgets = new Map<string, { turn: string; call: number; turnMaxBytes?: number }>()
  const writeBudget = async (session: string, turn: string, budgetFile: string, files: DownloadTarget['files']) => {
    const was = budgets.get(session)
    const now = was && was.turn === turn ? was : { turn, call: 0 }
    now.call++
    budgets.delete(session)
    budgets.set(session, now)
    if (budgets.size > OPTIONS_KEPT) budgets.delete(budgets.keys().next().value!)
    try {
      const ref = { kind: 'session' as const, sessionId: session }
      const [limits, usage] = await Promise.all([files.limits(), files.usage(ref)])
      const room = Math.max(0, limits.workspaceMaxBytes - usage.bytes)
      now.turnMaxBytes ??= room
      writeDownloadBudget(budgetFile, {
        call: now.call,
        turn,
        fileMaxBytes: Math.min(limits.fileMaxBytes, room),
        callMaxBytes: room,
        callMaxCount: MAX_DOWNLOADS_PER_CALL,
        turnMaxBytes: now.turnMaxBytes,
        turnMaxCount: MAX_DOWNLOADS_PER_TURN,
      })
    } catch {
      // The workspace could not be read: nothing downloads in this call.
      writeDownloadBudget(budgetFile, { ...NO_DOWNLOADS, call: now.call, turn })
    }
  }
  return async (context) => {
    if ((await deps.settings?.get<unknown>(SETTING_HEADLESS_BROWSER)) !== true) return []
    const policy = deps.builtIn ? await deps.builtIn() : undefined
    // Read only once the browser's own server is asked (`ready`), never for another server's call.
    type State = { options: HeadlessBrowserOptions; approved: Set<string>; known: ReturnType<typeof browserOrigins> }
    let state: State | undefined
    let reading: Promise<State> | undefined
    const ready = async () => {
      state = await (reading ??= (async () => {
        const base = await turnOptions(context.session)
        // Approved origins are read now, every call, so one approved on another replica counts here.
        const approvedOrigins = base.browserAllowedOrigins ? await loadApprovedOrigins(deps.sql, context.session) : undefined
        const options = { ...base, ...(approvedOrigins ? { approvedOrigins } : {}) }
        // The origins its config allows, and the guard checks: computed as materializeHeadlessBrowser does.
        const known = browserOrigins({
          backendUrl: normaliseOrigin(options.backendUrl) ?? options.backendUrl,
          publicUrl: options.publicUrl,
          livePublicUrl: options.livePublicUrl,
          uiOrigins: options.uiOrigins,
          browserAllowed: options.browserAllowedOrigins,
          reach: options.uiReach,
        })
        return { options, approved: new Set(approvedOrigins ?? []), known }
      })())
    }
    const read = (): State => {
      if (!state) throw new Error('the headless browser was asked before its options were read')
      return state
    }
    // Started only when a browser tool is listed or called; each use is released (withClient).
    let held: Hosted | undefined
    const hosted = async () => (held = await deps.host.browser(context.session, read().options))
    const guard = (name: string, input: unknown) => browserInputGuard(name, input, read().known, read().approved)
    const tier = (name: string, input: unknown): RiskTier => {
      const verdict = guard(name, input)
      const base: RiskTier = verdict && 'outward' in verdict ? 'outward' : (browserTierOf(name) ?? 'outward')
      return policy ? policy.tierOf(name, base) : base
    }
    const browser: TurnServer = {
      name: BROWSER_SERVER,
      pinned: true,
      keepOpen: true,
      ready,
      connect: async () => (await hosted()).client,
      release: () => {
        if (held) deps.host.release(held)
        held = undefined
      },
      // The download budget for this call, and what it downloaded drained into the workspace
      // after it, as run.ts's PreToolUse and PostToolUse hooks do in a classic turn (#2402).
      // Never fails the call: a budget that cannot be written leaves the one there, at
      // most the last call's, and a new browser's none (as run.ts's hook).
      before: async () => {
        if (!held) return
        const files = deps.files
        try {
          if (!files) writeDownloadBudget(held.plugin.budgetFile, { ...NO_DOWNLOADS, call: null })
          else await writeBudget(context.session, context.turnId ?? 'unknown', held.plugin.budgetFile, files)
        } catch {
          // As above.
        }
      },
      after: async () => {
        if (!deps.files) return undefined
        const drained = await drainDownloads(
          browserDownloadsDir(sessionBrowserDir(deps.paths, context.session)),
          { files: deps.files, ref: { kind: 'session', sessionId: context.session } },
          { waitMs: DOWNLOAD_WAIT_MS },
        ).catch(() => undefined)
        return drained && describeDrained(drained)
      },
      tierOf: tier,
      // browser_navigate and browser_tabs are outward by the origin they open.
      mayPark: (name) => browserTierDependsOnInput(name) || tier(name, undefined) === 'outward',
      offers: (tool) => !DENIED.has(tool) && !policy?.isDisabled(PLUGIN_NAME, tool),
      refusal: (name, input) => {
        const verdict = guard(name, input)
        return verdict && 'deny' in verdict ? verdict.deny : undefined
      },
      approved: async (name, input, requestId) => {
        const { known, approved } = read()
        const origin = originToApprove(name, input, known, approved)
        if (origin === undefined) return
        await rememberDurableOrigin(deps.sql, context.session, origin, requestId)
        approved.add(origin)
        const h = await deps.host.browser(context.session, read().options)
        try {
          h.plugin.approve(origin)
        } finally {
          deps.host.release(h)
        }
      },
    }
    const grant: TurnServer = {
      name: GRANT_SERVER,
      connect: () => {
        const call = context.call
        // Listing needs no call; a call without one is never made (turnToolActivity passes it).
        return connectInProcess(
          () => durableGrantServer({ sql: deps.sql, sessionId: context.session, requestId: call?.requestId ?? '' }).instance,
        )
      },
      tierOf: (name) => browserTierOf(name) ?? 'outward',
      mayPark: (name) => (browserTierOf(name) ?? 'outward') === 'outward',
    }
    return [browser, grant]
  }
}
