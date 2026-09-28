import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Vetting a local plugin before the harness loads it (spec §8.6, "Malicious or
// changed plugin | ... command hooks refused"; §10, "Command hooks are
// refused, because the harness has no shell").
//
// Why it matters here: the harness passes the Claude credential to Claude Code
// through the SDK's `env` (run.ts), and every process Claude Code starts for a
// plugin inherits that environment. A plugin that runs a program of its own
// therefore gets ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN. `tools: []` removes
// Bash, but not these, which Claude Code starts itself.
//
// What starts a process, per the plugin manifest reference
// (https://code.claude.com/docs/en/plugins-reference, "Fields", "Component
// path forms" and "Standard layout") and the hooks reference
// (https://code.claude.com/docs/en/hooks, "Hook handler fields"):
//
//   - hooks: `hooks/hooks.json`, merged with the manifest's `hooks` ("Path,
//     object, or array of either"). Handlers have a `type` of `command`,
//     `http`, `mcp_tool`, `prompt` or `agent`; `command` runs a shell command.
//     Refused: any handler that is not one of the four non-command types, so a
//     missing or unknown type is refused too.
//   - MCP servers: `.mcp.json`, merged with the manifest's `mcpServers` ("Path,
//     object, or array of either": a `.json` file, an `.mcpb`/`.dxt` bundle or
//     bundle URL, or an inline map). A server with a `command`, or of type
//     `stdio`, is a local process. Refused: every server that is not
//     `type: "http"` or `"sse"` with a `url` and no `command`, and every bundle
//     (a bundle packages a local server).
//   - LSP servers: `.lsp.json`, merged with `lspServers`; each has a required
//     `command`. Refused when present.
//   - Monitors: `monitors/monitors.json`, or `experimental.monitors` (or a
//     legacy top-level `monitors`); each is a `command` run as a background
//     process. Refused when present.
//
// `bin/` is not checked: it only extends the Bash tool's PATH, and the harness
// has no Bash tool. Skills, agents and commands are Markdown and are loaded.
//
// The plugin is REFUSED as a whole rather than loaded with the offending parts
// stripped: stripping means rewriting someone's plugin into a copy, and a
// plugin that needed its hook to work is better reported than half-loaded.

export class PluginRefusedError extends Error {
  override name = 'PluginRefusedError'
  readonly problems: readonly string[]
  constructor(pluginPath: string, problems: readonly string[]) {
    super(`plugin ${pluginPath} is refused: ${problems.join('; ')}`)
    this.problems = problems
  }
}

type Json = unknown

const NON_COMMAND_HOOK_TYPES = new Set(['http', 'mcp_tool', 'prompt', 'agent'])
const REMOTE_MCP_TYPES = new Set(['http', 'sse'])

function isRecord(value: Json): value is Record<string, Json> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Reads a JSON file inside the plugin; a missing default file is `undefined`. */
function readJson(root: string, relative: string, problems: string[], required: boolean): Json {
  const file = path.resolve(root, relative)
  if (file !== root && !file.startsWith(root + path.sep)) {
    problems.push(`${relative} is outside the plugin`)
    return undefined
  }
  if (!existsSync(file)) {
    if (required) problems.push(`${relative} is declared but missing`)
    return undefined
  }
  try {
    return JSON.parse(readFileSync(file, 'utf8')) as Json
  } catch {
    problems.push(`${relative} is not valid JSON`)
    return undefined
  }
}

/** A manifest value that is a path, an inline value, or an array of either. */
function eachDeclared(value: Json): Json[] {
  if (value === undefined) return []
  return Array.isArray(value) ? value : [value]
}

function checkHooksConfig(config: Json, where: string, problems: string[]): void {
  if (config === undefined) return
  // A hooks file is `{ "hooks": { Event: [...] } }`; inline manifest hooks are
  // `{ Event: [...] }` (the settings.json shape). Accept both.
  const events = isRecord(config) && isRecord(config.hooks) ? config.hooks : config
  if (!isRecord(events)) {
    problems.push(`${where}: hooks are not an object`)
    return
  }
  for (const [event, groups] of Object.entries(events)) {
    for (const group of Array.isArray(groups) ? groups : [groups]) {
      const handlers = isRecord(group) ? group.hooks : undefined
      for (const handler of Array.isArray(handlers) ? handlers : [handlers]) {
        const type = isRecord(handler) ? handler.type : undefined
        if (typeof type !== 'string' || !NON_COMMAND_HOOK_TYPES.has(type)) {
          problems.push(`${where}: ${event} has a ${typeof type === 'string' ? `"${type}"` : 'command'} hook`)
        }
      }
    }
  }
}

function checkMcpMap(map: Json, where: string, problems: string[]): void {
  if (map === undefined) return
  // `.mcp.json` wraps the map in `mcpServers`; a manifest path is the map itself.
  const servers = isRecord(map) && isRecord(map.mcpServers) ? map.mcpServers : map
  if (!isRecord(servers)) {
    problems.push(`${where}: MCP servers are not an object`)
    return
  }
  for (const [name, server] of Object.entries(servers)) {
    const remote =
      isRecord(server) &&
      typeof server.type === 'string' &&
      REMOTE_MCP_TYPES.has(server.type) &&
      typeof server.url === 'string' &&
      server.command === undefined
    if (!remote) problems.push(`${where}: MCP server "${name}" is a local (stdio) server`)
  }
}

function checkManifestRefs(
  root: string,
  value: Json,
  field: string,
  problems: string[],
  checkInline: (inline: Json, where: string) => void,
): void {
  for (const entry of eachDeclared(value)) {
    if (typeof entry === 'string') {
      if (/^https?:\/\//.test(entry) || /\.(mcpb|dxt)$/i.test(entry)) {
        problems.push(`${field}: ${entry} is a bundle, which runs a local server`)
        continue
      }
      checkInline(readJson(root, entry, problems, true), entry)
    } else {
      checkInline(entry, `plugin.json ${field}`)
    }
  }
}

/** Every reason `pluginPath` may not be loaded; empty when it may. */
export function pluginProblems(pluginPath: string): string[] {
  const root = path.resolve(pluginPath)
  const problems: string[] = []
  let isDir = false
  try {
    isDir = statSync(root).isDirectory()
  } catch {
    // reported below
  }
  if (!isDir) return ['not a directory']

  const manifest = readJson(root, '.claude-plugin/plugin.json', problems, false)
  const m = isRecord(manifest) ? manifest : {}

  checkHooksConfig(readJson(root, 'hooks/hooks.json', problems, false), 'hooks/hooks.json', problems)
  checkManifestRefs(root, m.hooks, 'hooks', problems, (c, w) => checkHooksConfig(c, w, problems))

  checkMcpMap(readJson(root, '.mcp.json', problems, false), '.mcp.json', problems)
  checkManifestRefs(root, m.mcpServers, 'mcpServers', problems, (c, w) => checkMcpMap(c, w, problems))

  if (existsSync(path.join(root, '.lsp.json')) || m.lspServers !== undefined) {
    problems.push('declares LSP servers, which run local commands')
  }
  const experimental = isRecord(m.experimental) ? m.experimental : {}
  if (
    existsSync(path.join(root, 'monitors', 'monitors.json')) ||
    experimental.monitors !== undefined ||
    m.monitors !== undefined
  ) {
    problems.push('declares monitors, which run local commands')
  }
  return problems
}

/** Throws PluginRefusedError unless the plugin starts no local process. */
export function assertPluginAllowed(pluginPath: string): void {
  const problems = pluginProblems(pluginPath)
  if (problems.length > 0) throw new PluginRefusedError(pluginPath, problems)
}

/**
 * ScadBuddy's own plugin (#299; spec §10, "baked into the image and loaded by
 * path"): `/app/plugins/scadbuddy` in the image (Dockerfile `agent` stage
 * copies it beside `/app/agent`), and the repository's `plugins/scadbuddy` in
 * a checkout. Both are three levels above this module (`dist/harness/` or
 * `src/harness/`), so the path is fixed by the layout, not configured: spec §9
 * keeps AI configuration out of the environment.
 */
export const BUNDLED_PLUGIN_DIR = fileURLToPath(new URL('../../../plugins/scadbuddy', import.meta.url))

/**
 * The plugin paths every session's queries load: the bundled plugin, once it
 * passes the same vetting run.ts applies per query. Missing or refused, it is
 * left out and `log` says why, so sessions still run without its skills
 * rather than every turn failing on PluginRefusedError.
 *
 * Its `.mcp.json` (the remote `scadbuddy` server for Claude Code installs) is
 * not started in the harness: `strictMcpConfig` ignores "all other MCP
 * configurations: project `.mcp.json`, user settings, plugins" (sdk.d.ts
 * 0.3.283), so the harness reaches the same tools through its in-process
 * server (tools/harness.ts) instead of over /mcp.
 */
export function bundledPluginPaths(log: (message: string) => void, dir: string = BUNDLED_PLUGIN_DIR): string[] {
  const problems = pluginProblems(dir)
  if (problems.length > 0) {
    log(`the ScadBuddy plugin at ${dir} is not loaded: ${problems.join('; ')}`)
    return []
  }
  return [dir]
}
