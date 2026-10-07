import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

// Vetting a local plugin before the harness loads it (spec §8.6, "Malicious or
// changed plugin | ... command hooks refused"; §10, "Command hooks are
// refused, because the harness has no shell").
//
// Why it matters here: the harness passes the Claude credential to Claude Code
// through the SDK's `env` (run.ts), and every process Claude Code starts for a
// plugin inherits that environment. A plugin that runs a program of its own
// therefore gets ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN /
// ANTHROPIC_AUTH_TOKEN. `tools: []` removes Bash, but not these, which Claude
// Code starts itself.
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
//   - hooks modules: a hooks file's `modules` names a JavaScript module
//     (`export function register(on)`) that Claude Code runs itself, with
//     process, network and environment access (`$.process.run`, `$.http`,
//     `$.env.get`), so with the credential env. Claude Code 2.1.283 loaded one
//     only with CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1; 2.1.287 loads it by
//     default, and that variable no longer turns it off (measured 2026-10-06:
//     a module's prompt.context hook put the gateway token in the model
//     request). Refused when present, in any hooks config. A hooks file may
//     carry only the keys 2.1.287's hooks-file schema reads besides that one
//     (`$schema`, `description`, `hooks`; `surface` is gone), so a later CLI's
//     new loader key is refused by default.
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

/**
 * Whether `target` (an absolute, resolved path) is `root` or inside it. A
 * path-boundary test, not a string prefix: `<root>x` and `<root>/../x` are
 * outside. Every containment check on plugin-supplied paths goes through it
 * (here and in src/plugins/packages/vet.ts).
 */
export function isInside(root: string, target: string): boolean {
  const base = path.resolve(root)
  const resolved = path.resolve(target)
  return resolved === base || resolved.startsWith(base.endsWith(path.sep) ? base : base + path.sep)
}

const OUTSIDE = ' is outside the plugin'

/**
 * Whether a problem names a path that leaves the plugin. A plugin package's
 * admin can allow what the rules refuse (src/plugins/packages/vet.ts), but not
 * this: such a file is not part of the pinned, hashed package.
 */
export function isOutsideProblem(problem: string): boolean {
  return problem.endsWith(OUTSIDE)
}

/** Reads a JSON file inside the plugin; a missing default file is `undefined`. */
function readJson(root: string, relative: string, problems: string[], required: boolean): Json {
  const file = path.resolve(root, relative)
  if (!isInside(root, file)) {
    problems.push(`${relative}${OUTSIDE}`)
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

/** The keys a hooks file may carry besides `modules`, which is refused on its own. */
const HOOKS_FILE_KEYS = new Set(['$schema', 'description', 'hooks'])

/**
 * A hooks config's events. A hooks file is `{ "hooks": { Event: [...] },
 * "modules"?: [...] }`; inline manifest hooks are `{ Event: [...] }` (the
 * settings.json shape). Both are accepted (also by src/plugins/packages/vet.ts).
 * Any hooks-file key makes it a file, whatever its value: no event has one of
 * those names, and judging by the value's shape let `{ "hooks": [...],
 * "loaders": [...] }` pass as two inline events, skipping the key check.
 */
export function hookEvents(config: Json): { file: boolean; events: Json } {
  const file = isRecord(config) && ['modules', ...HOOKS_FILE_KEYS].some((key) => key in config)
  return { file, events: file ? (config.hooks === undefined ? {} : config.hooks) : config }
}

function checkHooksConfig(config: Json, where: string, problems: string[]): void {
  if (config === undefined) return
  if (isRecord(config) && config.modules !== undefined) {
    problems.push(`${where}: names a hooks module, which runs JavaScript inside Claude Code`)
  }
  const { file, events } = hookEvents(config)
  if (file && isRecord(config)) {
    for (const key of Object.keys(config)) {
      if (key !== 'modules' && !HOOKS_FILE_KEYS.has(key)) problems.push(`${where}: a hooks file may not set "${key}"`)
    }
  }
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

/** The plugin's hook configs and MCP maps, each with where it was declared (for plugin packages' extra vetting). */
export type DeclaredConfigs = {
  manifest: Record<string, Json>
  hooks: { where: string; config: Json }[]
  mcp: { where: string; map: Json }[]
  problems: string[]
}

/**
 * Reads what `pluginProblems` checks, for callers that check more
 * (src/plugins/packages/vet.ts). Bundles are skipped here: pluginProblems
 * refuses them.
 */
export function declaredConfigs(pluginPath: string): DeclaredConfigs {
  const root = path.resolve(pluginPath)
  const problems: string[] = []
  const manifest = readJson(root, '.claude-plugin/plugin.json', problems, false)
  const m = isRecord(manifest) ? manifest : {}
  const hooks: DeclaredConfigs['hooks'] = []
  const mcp: DeclaredConfigs['mcp'] = []
  const collect = (value: Json, field: string, into: (config: Json, where: string) => void) => {
    for (const entry of eachDeclared(value)) {
      if (typeof entry === 'string') {
        if (/^https?:\/\//.test(entry) || /\.(mcpb|dxt)$/i.test(entry)) continue
        into(readJson(root, entry, problems, true), entry)
      } else {
        into(entry, `plugin.json ${field}`)
      }
    }
  }
  const hooksFile = readJson(root, 'hooks/hooks.json', problems, false)
  if (hooksFile !== undefined) hooks.push({ where: 'hooks/hooks.json', config: hooksFile })
  collect(m.hooks, 'hooks', (config, where) => hooks.push({ where, config }))
  const mcpFile = readJson(root, '.mcp.json', problems, false)
  if (mcpFile !== undefined) mcp.push({ where: '.mcp.json', map: mcpFile })
  collect(m.mcpServers, 'mcpServers', (map, where) => mcp.push({ where, map }))
  return { manifest: m, hooks, mcp, problems }
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
