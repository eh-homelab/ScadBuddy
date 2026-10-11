import { readFileSync } from 'node:fs'
import path from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import type { Sql } from 'postgres'
import { browserOrigins, loadApprovedOrigins, rememberDurableOrigin } from '../harness/browserOrigins.js'
import type { UiReachMap } from '../harness/browserReach.js'
import {
  assertHeadlessPlugin,
  browserInputGuard,
  browserTierOf,
  DISALLOWED_BROWSER_TOOLS,
  GRANT_SERVER,
  type HeadlessBrowserOptions,
  type HeadlessBrowserPlugin,
  materializeHeadlessBrowser,
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
// process. It is closed once the session has not used it for BROWSER_IDLE_MS.
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

export type Hosted = { client: Client; plugin: HeadlessBrowserPlugin; key: string; lastUsed: number }

/** The browsers this replica runs for durable sessions, one per session. */
export class BrowserHost {
  readonly #paths: HarnessPaths
  readonly #idleMs: number
  readonly #hosted = new Map<string, Promise<Hosted>>()
  readonly #timer: NodeJS.Timeout

  constructor(options: { paths: HarnessPaths; idleMs?: number }) {
    this.#paths = options.paths
    this.#idleMs = options.idleMs ?? BROWSER_IDLE_MS
    this.#timer = setInterval(() => void this.reap(), Math.min(this.#idleMs, 60_000))
    this.#timer.unref()
  }

  /** The session's browser, started with these options, or the running one if they are the same. */
  async browser(session: string, options: HeadlessBrowserOptions): Promise<Hosted> {
    const key = JSON.stringify({ ...options, approvedOrigins: undefined, rememberOrigin: undefined })
    const running = this.#hosted.get(session)
    if (running) {
      const hosted = await running.catch(() => undefined)
      if (hosted && hosted.key === key) {
        hosted.lastUsed = Date.now()
        return hosted
      }
      // Its origins or sandbox changed since (a setting): started again with the new ones.
      await this.close(session)
    }
    const starting = this.#start(options, key)
    this.#hosted.set(session, starting)
    starting.catch(() => this.#hosted.delete(session))
    return starting
  }

  async #start(options: HeadlessBrowserOptions, key: string): Promise<Hosted> {
    const plugin = materializeHeadlessBrowser(options)
    assertHeadlessPlugin(plugin.pluginDir)
    const config = JSON.parse(readFileSync(path.join(plugin.pluginDir, '.mcp.json'), 'utf8')) as {
      mcpServers: Record<string, { command: string; args: string[] }>
    }
    const server = config.mcpServers[SERVER_NAME]!
    const client = new Client({ name: 'scadbuddy-durable-browser', version: '1' })
    // `env -i` in the command clears what the transport would pass on (serverCommand).
    await client.connect(new StdioClientTransport({ command: server.command, args: server.args, stderr: 'ignore' }))
    return { client, plugin, key, lastUsed: Date.now() }
  }

  async close(session: string): Promise<void> {
    const hosted = await this.#hosted.get(session)?.catch(() => undefined)
    this.#hosted.delete(session)
    await hosted?.client.close().catch(() => {})
    await removeSessionBrowserDirs(this.#paths, session).catch(() => {})
  }

  async reap(now = Date.now()): Promise<void> {
    for (const [session, hosted] of this.#hosted) {
      const h = await hosted.catch(() => undefined)
      if (!h || now - h.lastUsed >= this.#idleMs) await this.close(session)
    }
  }

  async stop(): Promise<void> {
    clearInterval(this.#timer)
    await Promise.all([...this.#hosted.keys()].map((s) => this.close(s)))
  }
}

export type TurnBrowserDeps = {
  sql: Sql
  paths: HarnessPaths
  host: Pick<BrowserHost, 'browser'>
  headless: HeadlessBrowserDeps
  settings?: { get<T>(key: string): Promise<T | undefined> } | undefined
  builtIn?: (() => Promise<BuiltInPolicy>) | undefined
}

/** The options a classic turn would start this session's browser with (sessions/manager.ts `runTurn`). */
async function optionsFor(deps: TurnBrowserDeps, session: string): Promise<HeadlessBrowserOptions> {
  const hb = deps.headless
  const sandbox = hb.sandbox ? await hb.sandbox() : false
  const livePublicUrl = hb.livePublicUrl ? await hb.livePublicUrl().catch(() => undefined) : undefined
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
    ...(hb.browserAllowedOrigins
      ? { browserAllowedOrigins: hb.browserAllowedOrigins, approvedOrigins: await loadApprovedOrigins(deps.sql, session) }
      : {}),
    dir: sessionBrowserDir(deps.paths, session),
    tmpDir: sessionBrowserTmpDir(session),
    ...(hb.executablePath ? { executablePath: hb.executablePath } : {}),
  }
}

const DENIED = new Set<string>(DISALLOWED_BROWSER_TOOLS)

/** The headless browser's turn servers, when Settings has it on; none otherwise. */
export function turnBrowserServers(deps: TurnBrowserDeps): (context: TurnContext) => Promise<TurnServer[]> {
  return async (context) => {
    if ((await deps.settings?.get<unknown>(SETTING_HEADLESS_BROWSER)) !== true) return []
    const policy = deps.builtIn ? await deps.builtIn() : undefined
    const options = await optionsFor(deps, context.session)
    // Approved origins are read now, every call, so one approved on another replica counts here.
    const approved = new Set(options.approvedOrigins ?? [])
    // Started only when a browser tool is listed or called, never for another server's call.
    const hosted = () => deps.host.browser(context.session, options)
    // The origins its config allows, and the guard checks: computed as materializeHeadlessBrowser does.
    const known = browserOrigins({
      backendUrl: normaliseOrigin(options.backendUrl) ?? options.backendUrl,
      publicUrl: options.publicUrl,
      livePublicUrl: options.livePublicUrl,
      uiOrigins: options.uiOrigins,
      browserAllowed: options.browserAllowedOrigins,
      reach: options.uiReach,
    })
    const guard = (name: string, input: unknown) => browserInputGuard(name, input, known, approved)
    const tier = (name: string, input: unknown): RiskTier => {
      const verdict = guard(name, input)
      const base: RiskTier = verdict && 'outward' in verdict ? 'outward' : (browserTierOf(name) ?? 'outward')
      return policy ? policy.tierOf(name, base) : base
    }
    const browser: TurnServer = {
      name: BROWSER_SERVER,
      pinned: true,
      keepOpen: true,
      connect: async () => (await hosted()).client,
      tierOf: tier,
      offers: (tool) => !DENIED.has(tool) && !policy?.isDisabled(PLUGIN_NAME, tool),
      refusal: (name, input) => {
        const verdict = guard(name, input)
        return verdict && 'deny' in verdict ? verdict.deny : undefined
      },
      approved: async (name, input, requestId) => {
        const origin = originToApprove(name, input, known, approved)
        if (origin === undefined) return
        await rememberDurableOrigin(deps.sql, context.session, origin, requestId)
        approved.add(origin)
        ;(await hosted()).plugin.approve(origin)
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
    }
    return [browser, grant]
  }
}
