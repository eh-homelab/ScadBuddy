import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { normaliseOrigin } from '../http/origins.js'
import { isUuid } from './stateDirs.js'
import type { RiskTier } from './permissions.js'

// The headless browser (issue #349, spec D11 and §5.3): the official `playwright`
// Claude plugin, pinned and vendored, loaded into a session's harness by local
// path. It drives a headless Chromium inside the agent container for sessions
// that have no user tab (#300 sessions over /mcp, authoring checks, evals). The
// #254 bridge stays the only way to touch the user's own tab.
//
// SOURCES
//   - The plugin: anthropics/claude-plugins-official, external_plugins/playwright,
//     read at commit fa59bc9037741ecfa131aa27938272605710d7b2
//     (https://github.com/anthropics/claude-plugins-official/tree/fa59bc9037741ecfa131aa27938272605710d7b2/external_plugins/playwright).
//     Its `.mcp.json` is `{"playwright": {"command": "npx", "args":
//     ["@playwright/mcp@latest"]}}` and its `plugin.json` names the author
//     Microsoft. The repository is Apache-2.0 (its root LICENSE). The manifest
//     is vendored verbatim in agent/plugins/playwright/; the `.mcp.json` is
//     NOT used as shipped (spec D11: `npx …@latest` fetches an unpinned package
//     at runtime) and is written per session below instead.
//   - The server: `@playwright/mcp` 0.0.82, an exact dependency of agent/
//     (Apache-2.0, https://www.npmjs.com/package/@playwright/mcp/v/0.0.82,
//     https://github.com/microsoft/playwright-mcp). Every option below is read
//     from that version's `config.d.ts` and README ("Configuration").
//
// WHAT A SESSION GETS (spec §5.3), measured in test/headlessBrowser.server.test.ts
// and test/headlessBrowser.e2e.test.ts (docs/ai/headless-browser.md, "Measured"):
//   - `--headless`, `--isolated` (profile in memory, gone with the session),
//     no `--user-data-dir`, no `--storage-state`;
//   - `--config <file>` with `network.allowedOrigins` = the backend's origin
//     (SCADBUDDY_BACKEND_URL serves the SPA) and nothing else;
//   - `outputDir` = `output/` in the session's browser directory, never
//     `--allow-unrestricted-file-access`, `capabilities` left at the core set
//     (no `--caps`), `webmcp: false` (`--no-webmcp`);
//   - `browser.contextOptions.extraHTTPHeaders` carries the agent-actor marker
//     (AGENT_ACTOR_HEADER) naming the session; the backend refuses outward
//     routes on requests that carry it (backend/scadbuddy/api/agent_actor.py);
//   - the server process starts under `env -i` with only the variables it
//     needs, so it does NOT inherit Claude Code's environment, which holds the
//     Claude credential (run.ts `credentialEnv`). That is why plugins.ts
//     refuses every other stdio server; this one is built here, not read from
//     someone's plugin, and assertHeadlessPlugin() checks it before loading.
//
// Guards enforced by the harness, not by the server (permissions.ts seam):
//   - the four tools spec §5.3 names (and `browser_install`, which downloads a
//     browser) are in `disallowedTools`, so the model never sees them;
//   - every URL a tool takes (`browser_navigate`, `browser_tabs` new) must be
//     on the allowed origin: `allowedOrigins` "does not serve as a security
//     boundary and does not affect redirects" (README), so the hook refuses
//     other origins before the server is asked;
//   - every `filename` a tool takes must be a plain file name: files with an
//     explicit name "are resolved against the workspace root" (README), and a
//     name with a directory part could reach anywhere under it.

/** The pinned `@playwright/mcp` version; agent/package.json pins the same. */
export const PLAYWRIGHT_MCP_VERSION = '0.0.82'

/** The official plugin's commit the vendored manifest was read at. */
export const PLAYWRIGHT_PLUGIN_COMMIT = 'fa59bc9037741ecfa131aa27938272605710d7b2'

/** The plugin's name (its plugin.json) and its MCP server's name (its `.mcp.json`). */
export const PLUGIN_NAME = 'playwright'
export const SERVER_NAME = 'playwright'

/**
 * The prefix Claude Code gives a plugin MCP server's tools:
 * `mcp__plugin_<plugin>_<server>__<tool>`. Measured on the bundled Claude Code
 * 2.1.283 (the init message's `tools`, test/headlessBrowser.e2e.test.ts).
 */
export const TOOL_PREFIX = `mcp__plugin_${PLUGIN_NAME}_${SERVER_NAME}__`

/**
 * The agent-actor marker (spec §5.3): sent on every request the headless
 * context makes, naming the session. It is not authentication; a request
 * carrying it can only be refused where one without it would not be.
 * backend/scadbuddy/api/agent_actor.py reads the same name.
 */
export const AGENT_ACTOR_HEADER = 'X-ScadBuddy-Agent-Session'

/** The `ai_settings` key that turns the headless browser on (spec §5.3, "Off unless enabled"). */
export const SETTING_HEADLESS_BROWSER = 'headless_browser_enabled'

/** Tools the model never sees (spec §5.3, "Tools the model can't see"). */
export const DISALLOWED_BROWSER_TOOLS = [
  'browser_run_code_unsafe', // "executes arbitrary JavaScript in the Playwright server process and is RCE-equivalent"
  'browser_evaluate', // JavaScript in the page
  'browser_file_upload', // takes absolute file paths
  'browser_drop', // takes absolute file paths
  'browser_install', // downloads a browser at runtime
] as const

/**
 * The explicit tier map (spec §5.3, "Tiers"), so the §8.1 default of
 * `outward` for unknown plugin tools never applies to these. Anything the
 * server adds in a later version is NOT here and so is `outward`: denied
 * until someone reviews it and adds it.
 */
export const BROWSER_TOOL_TIERS: Readonly<Record<string, RiskTier>> = {
  // read: navigating, snapshots, screenshots, console and network listings, waiting
  browser_navigate: 'read',
  browser_navigate_back: 'read',
  browser_snapshot: 'read',
  browser_take_screenshot: 'read',
  browser_console_messages: 'read',
  browser_network_requests: 'read',
  browser_network_request: 'read',
  browser_wait_for: 'read',
  browser_find: 'read',
  // write: clicking, typing, filling, selecting, pressing keys, closing
  browser_click: 'write',
  browser_type: 'write',
  browser_fill_form: 'write',
  browser_select_option: 'write',
  browser_press_key: 'write',
  browser_hover: 'write',
  browser_drag: 'write',
  browser_handle_dialog: 'write',
  browser_resize: 'write',
  browser_emulate_media: 'write',
  browser_close: 'write',
  // One tool lists, opens, selects and closes tabs; opening one navigates, so
  // it is `write` as a whole rather than `read` for listing only.
  browser_tabs: 'write',
}

/**
 * The in-process tool that asks a human to allow ONE outward request from the
 * headless browser (headlessGrants.ts). Outward tier: it parks for approval.
 */
export const GRANT_SERVER = 'scadbuddy_browser'
export const AUTHORIZE_TOOL = 'authorize_request'
export const AUTHORIZE_TOOL_NAME = `mcp__${GRANT_SERVER}__${AUTHORIZE_TOOL}`

/** The tier of a headless-browser tool as the SDK names it; undefined for any other tool. */
export function browserTierOf(toolName: string): RiskTier | undefined {
  if (toolName === AUTHORIZE_TOOL_NAME) return 'outward'
  if (!toolName.startsWith(TOOL_PREFIX)) return undefined
  const bare = toolName.slice(TOOL_PREFIX.length)
  return Object.hasOwn(BROWSER_TOOL_TIERS, bare) ? BROWSER_TOOL_TIERS[bare] : undefined
}

/** `disallowedTools` entries: the full SDK names. */
export function disallowedBrowserTools(): string[] {
  return DISALLOWED_BROWSER_TOOLS.map((name) => `${TOOL_PREFIX}${name}`)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Only `name.ext`-like names: no directory part, no leading dot, no `..`. */
const PLAIN_FILE_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/

/**
 * Why a headless-browser tool call must not run, or undefined when it may.
 * Checked in the PreToolUse hook and canUseTool (permissions.ts) before the
 * server sees the call. Other tools are not this function's business.
 */
export function browserInputProblem(toolName: string, input: unknown, allowedOrigin: string): string | undefined {
  if (!toolName.startsWith(TOOL_PREFIX)) return undefined
  const bare = toolName.slice(TOOL_PREFIX.length)
  const args = isRecord(input) ? input : {}
  const url = bare === 'browser_navigate' || bare === 'browser_tabs' ? args.url : undefined
  if (url !== undefined) {
    const origin = typeof url === 'string' ? normaliseOrigin(url) : undefined
    if (origin !== allowedOrigin) {
      return (
        `${bare} may only open ScadBuddy's own UI at ${allowedOrigin}; ` +
        `${typeof url === 'string' ? JSON.stringify(url) : 'that URL'} is not on it, so it was not opened.`
      )
    }
  }
  const filename = args.filename
  if (filename !== undefined && (typeof filename !== 'string' || !PLAIN_FILE_NAME.test(filename) || filename.includes('..'))) {
    return `${bare}: filename must be a plain file name such as "preview.png", with no directory part.`
  }
  return undefined
}

// -- the per-session plugin ------------------------------------------------------

export type HeadlessBrowserOptions = {
  /** The session id (a UUID); it names the marker and the directory. */
  sessionId: string
  /** The one origin the browser may open: the backend's, which serves the SPA. */
  backendUrl: string
  /** Where this session's plugin copy, config and output files go; created if missing. */
  dir: string
  /**
   * PLAYWRIGHT_BROWSERS_PATH for the server. Defaults to this process's own,
   * which the image sets (Dockerfile `agent`); `env -i` would drop it otherwise.
   */
  browsersPath?: string
  /**
   * A Chromium to launch instead of the one `playwright` expects at
   * `browsersPath`. Tests only, for a machine with another revision installed.
   */
  executablePath?: string
}

export type HeadlessBrowserPlugin = {
  /** The plugin directory to load (`plugins: [{ type: 'local', path }]`). */
  pluginDir: string
  /** The server's config file. */
  configFile: string
  /** Where screenshots and other output land. */
  outputDir: string
  /** The origin in `network.allowedOrigins`, normalised. */
  allowedOrigin: string
}

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** agent/plugins/playwright, from src/harness or dist/harness alike. */
export const VENDORED_PLUGIN_DIR = path.resolve(HERE, '..', '..', 'plugins', PLUGIN_NAME)

/** The vendored server's entry point, resolved from agent/'s own node_modules. */
export function playwrightMcpCli(): string {
  const require = createRequire(import.meta.url)
  const pkgJson = require.resolve('@playwright/mcp/package.json')
  const pkg = JSON.parse(readFileSync(pkgJson, 'utf8')) as { version?: string }
  if (pkg.version !== PLAYWRIGHT_MCP_VERSION) {
    throw new Error(`@playwright/mcp is ${pkg.version ?? 'unknown'}, expected ${PLAYWRIGHT_MCP_VERSION}`)
  }
  return path.join(path.dirname(pkgJson), 'cli.js')
}

/** The server's `--config` file contents (config.d.ts `Config`, 0.0.82). */
export function playwrightConfig(options: {
  sessionId: string
  allowedOrigin: string
  outputDir: string
  executablePath?: string
}): Record<string, unknown> {
  return {
    browser: {
      browserName: 'chromium',
      isolated: true,
      // With `browserName` set and no `channel`, @playwright/mcp 0.0.82 does
      // not default to branded Chrome, and Playwright launches its
      // chromium-headless-shell (the one build the image installs, Dockerfile
      // `agent`). It then leaves `chromiumSandbox` false on Linux (its
      // `validateBrowserConfig`), i.e. Chromium runs with `--no-sandbox`.
      launchOptions: {
        headless: true,
        ...(options.executablePath ? { executablePath: options.executablePath } : {}),
      },
      contextOptions: {
        extraHTTPHeaders: { [AGENT_ACTOR_HEADER]: options.sessionId },
        acceptDownloads: false,
        serviceWorkers: 'block',
      },
    },
    network: { allowedOrigins: [options.allowedOrigin] },
    outputDir: options.outputDir,
    allowUnrestrictedFileAccess: false,
    webmcp: false,
    saveSession: false,
    imageResponses: 'allow',
  }
}

/** The one server in the per-session `.mcp.json`. */
export function serverCommand(options: {
  configFile: string
  home: string
  browsersPath?: string
  tmpDir?: string
}): { command: string; args: string[] } {
  // TMPDIR stays SHORT (the service's own, /tmp in the image): Chromium's
  // profile goes under it and its SingletonSocket is a Unix socket, whose path
  // is limited to ~107 bytes. Measured: under the session's browser directory
  // (`…/browser/<uuid>/home/tmp/playwright_chromiumdev_profile-XXXXXX/…`) the
  // launch fails with "Target page, context or browser has been closed".
  const env = [`HOME=${options.home}`, `TMPDIR=${options.tmpDir ?? os.tmpdir()}`]
  if (options.browsersPath) env.push(`PLAYWRIGHT_BROWSERS_PATH=${options.browsersPath}`)
  return {
    // `env -i` clears the inherited environment (coreutils env(1): "-i, --ignore-environment
    // start with an empty environment") before node starts, so the credential
    // Claude Code holds never reaches the server or Chromium.
    command: '/usr/bin/env',
    args: [
      '-i',
      ...env,
      process.execPath,
      playwrightMcpCli(),
      '--config',
      options.configFile,
      '--headless',
      '--isolated',
      '--no-webmcp',
      '--block-service-workers',
    ],
  }
}

/**
 * Writes the session's copy of the plugin: the vendored manifest, a `.mcp.json`
 * that starts the pinned server with the session's config, and that config.
 * Synchronous (a few small files) so buildHarnessOptions stays synchronous.
 */
export function materializeHeadlessBrowser(options: HeadlessBrowserOptions): HeadlessBrowserPlugin {
  if (!isUuid(options.sessionId)) throw new Error(`not a session id: ${JSON.stringify(options.sessionId)}`)
  const allowedOrigin = normaliseOrigin(options.backendUrl)
  if (!allowedOrigin) throw new Error(`not an http(s) origin: ${options.backendUrl}`)

  const dir = path.resolve(options.dir)
  const pluginDir = path.join(dir, 'plugin')
  const outputDir = path.join(dir, 'output')
  const home = path.join(dir, 'home')
  const configFile = path.join(dir, 'playwright-mcp.json')
  for (const d of [path.join(pluginDir, '.claude-plugin'), outputDir, home]) {
    mkdirSync(d, { recursive: true })
  }

  const manifest = readFileSync(path.join(VENDORED_PLUGIN_DIR, '.claude-plugin', 'plugin.json'), 'utf8')
  writeFileSync(path.join(pluginDir, '.claude-plugin', 'plugin.json'), manifest)
  writeFileSync(
    configFile,
    JSON.stringify(
      playwrightConfig({
        sessionId: options.sessionId,
        allowedOrigin,
        outputDir,
        ...(options.executablePath ? { executablePath: options.executablePath } : {}),
      }),
      null,
      2,
    ),
  )
  const browsersPath = options.browsersPath ?? process.env.PLAYWRIGHT_BROWSERS_PATH
  const server = serverCommand({ configFile, home, ...(browsersPath ? { browsersPath } : {}) })
  writeFileSync(
    path.join(pluginDir, '.mcp.json'),
    JSON.stringify({ mcpServers: { [SERVER_NAME]: { type: 'stdio', ...server } } }, null, 2),
  )
  return { pluginDir, configFile, outputDir, allowedOrigin }
}

/**
 * Checks a materialized plugin before loading it: exactly one server, started
 * under `env -i`, running the pinned cli.js, and none of the flags spec §5.3
 * rules out. Throws otherwise. It stands in for plugins.ts `assertPluginAllowed`,
 * which refuses every stdio server.
 */
export function assertHeadlessPlugin(pluginDir: string): void {
  const problems: string[] = []
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(path.join(pluginDir, '.mcp.json'), 'utf8'))
  } catch {
    throw new Error(`headless browser plugin ${pluginDir}: .mcp.json is missing or not JSON`)
  }
  const servers = isRecord(parsed) && isRecord(parsed.mcpServers) ? parsed.mcpServers : {}
  const names = Object.keys(servers)
  if (names.length !== 1 || names[0] !== SERVER_NAME) problems.push(`expected one server "${SERVER_NAME}", got ${JSON.stringify(names)}`)
  const server = servers[SERVER_NAME]
  const args = isRecord(server) && Array.isArray(server.args) ? server.args.map(String) : []
  if (!isRecord(server) || server.command !== '/usr/bin/env' || args[0] !== '-i') {
    problems.push('the server must start under `env -i`')
  }
  if (!args.includes(playwrightMcpCli())) problems.push('the server must run the pinned @playwright/mcp cli.js')
  for (const flag of ['--headless', '--isolated', '--no-webmcp', '--config']) {
    if (!args.includes(flag)) problems.push(`missing ${flag}`)
  }
  for (const flag of ['--allow-unrestricted-file-access', '--user-data-dir', '--storage-state', '--caps', '--extension']) {
    if (args.some((a) => a === flag || a.startsWith(`${flag}=`))) problems.push(`${flag} is not allowed`)
  }
  if (problems.length) throw new Error(`headless browser plugin ${pluginDir} is refused: ${problems.join('; ')}`)
}
