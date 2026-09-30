import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { isMap, isNode, isScalar, parseDocument, visit } from 'yaml'
import { declaredConfigs, isInside, pluginProblems } from '../../harness/plugins.js'
import { PLUGIN_NAME_RE, RESERVED_PLUGIN_NAMES } from '../registry.js'

// Vetting a plugin PACKAGE (issue #297, "Review before enable"), on top of the
// harness's own rules for any local plugin (src/harness/plugins.ts
// `pluginProblems`: no command hooks, no stdio/bundled MCP servers, no LSP
// servers, no monitors). A package is someone else's code fetched from the
// network, so it gets these as well, each with its source:
//
//   - Dynamic context injection. A skill or command line starting `` !`cmd` ``,
//     or a ```` ```! ```` block, is run by a shell "before the skill content is
//     sent to Claude" (https://code.claude.com/docs/en/skills, dynamic context
//     injection), in the harness's environment, which holds the Claude
//     credential. Every query sets `disableSkillShellExecution`
//     (harness/options.ts), so the CLI puts a placeholder in place of both
//     forms (measured on CLI 2.1.283, test/pluginPackages.e2e.test.ts; before
//     that, the harness denied the Bash call). Refused here as well, in every
//     Markdown file, so no review shows a package that relies on a shell.
//   - Frontmatter `hooks` (skills register hooks "when the skill is invoked",
//     same page, frontmatter fields), `mcpServers` and `permissionMode`
//     (subagent frontmatter): refused, so every hook and MCP server is in
//     the files vetted below and shown in the review. Frontmatter must be
//     plain YAML (`frontmatter()`), so no key can hide from this check.
//   - Tools. `allowed-tools` (skills, commands: "Tools Claude can use without
//     asking permission") and `tools` (subagents) may name MCP tools only
//     (`mcp__<server>__<tool>`), whose tier the permission seam decides
//     (harness/permissions.ts). A built-in such as `Bash(...)` or `Write` is
//     refused: the harness offers none (`tools: []`), and a package that asks
//     for one was written for a shell.
//   - MCP servers. `type: "http"` only (spec D5; the SDK's `sse` is the legacy
//     transport), no `headersHelper` (it runs a command,
//     https://code.claude.com/docs/en/mcp "Use dynamic headers"), and no `$`
//     anywhere: `${...}` resolves in an http server's `url` and `headers`
//     (https://code.claude.com/docs/en/plugins-reference, "Where each variable
//     resolves"), and environment expansion could put the credential in a
//     header sent to the plugin's server. Each URL is returned in `endpoints`
//     for the egress check (install.ts), at install and at every load.
//   - Hooks. `mcp_tool` hooks are refused: they call a tool from the hook
//     runner, and the hooks reference ("MCP tool hook fields") does not say
//     that call goes through permission checks; ours is not verified.
//     `http` hooks: no `$`, no `allowedEnvVars` (the variables a header may
//     interpolate, https://code.claude.com/docs/en/hooks "HTTP hook fields"),
//     and the URL goes through the egress check. `prompt` and `agent` hooks
//     run no code. Only the events in PACKAGE_HOOK_EVENTS are allowed: a
//     PermissionRequest or PreToolUse hook could approve or rewrite a call.
//   - Manifest fields ScadBuddy does not apply and a headless run cannot
//     honour: `dependencies` (other plugins, resolved by Claude Code's own
//     installer), `userConfig` and `channels` (prompted interactively and kept
//     in settings.json; ScadBuddy keeps plugin settings in Postgres),
//     `settings` and a root `settings.json` (their `agent` key replaces the
//     main thread's agent), and `workflows` (JavaScript).

export type ReviewHook = { event: string; type: string; url?: string }
export type ReviewMcpServer = { name: string; type: string; url: string }

/** What Settings shows before the admin approves a pin. */
export type PackageReview = {
  name: string
  description: string | null
  version: string | null
  /** As the harness names them: `<plugin>:<skill>`. */
  skills: string[]
  commands: string[]
  agents: string[]
  hooks: ReviewHook[]
  mcp_servers: ReviewMcpServer[]
  /** Every Markdown and JSON file, for the admin to read. */
  files: string[]
}

export type Endpoint = { what: string; url: string }

export type Vetting = {
  /** Undefined when the package has no usable name. */
  review: PackageReview | undefined
  problems: string[]
  endpoints: Endpoint[]
}

const NON_COMMAND_MCP_TYPE = 'http'

// The hook events a package may use: an allowlist, so an event added to
// Claude Code later is refused until it is read. Left out on purpose, from
// the CLI 2.1.283 event list and the hooks reference
// (https://code.claude.com/docs/en/hooks):
//   - PermissionRequest: the CLI races these against the host's
//     `can_use_tool` answer, and a hook's `behavior: "allow"` (or
//     `updatedInput`) wins, so an http hook would approve an outward tool
//     before the human does (spec §8.2).
//   - PreToolUse: `permissionDecision` and `updatedInput`, which would rewrite
//     a write-tier call that the harness then allows.
//   - PermissionDenied (`retry`), Elicitation/ElicitationResult (answers on the
//     user's behalf), WorktreeCreate/WorktreeRemove, ConfigChange, Setup,
//     the model-switch, file, directory, task and teammate events,
//     InstructionsLoaded, UserPromptExpansion, MessageDisplay and
//     PostToolBatch: decisions or side effects ScadBuddy has not reviewed.
const PACKAGE_HOOK_EVENTS = new Set([
  'SessionStart',
  'SessionEnd',
  'UserPromptSubmit',
  'PostToolUse',
  'PostToolUseFailure',
  'Notification',
  'Stop',
  'StopFailure',
  'SubagentStart',
  'SubagentStop',
  'PreCompact',
  'PostCompact',
])
const MAX_REPORTED_PROBLEMS = 50

/**
 * The fields of a hook or MCP server config whose key or value holds a `$`,
 * as dotted paths (`headers.Authorization`), so a refusal says where. Every
 * field is scanned, not just the ones known to expand: fail closed.
 */
export function dollarFields(value: unknown, at = ''): string[] {
  if (typeof value === 'string') return value.includes('$') ? [at || '(value)'] : []
  if (Array.isArray(value)) return value.flatMap((v, i) => dollarFields(v, `${at}[${i}]`))
  if (!isRecord(value)) return []
  return Object.entries(value).flatMap(([k, v]) => {
    const field = at ? `${at}.${k}` : k
    return k.includes('$') ? [field] : dollarFields(v, field)
  })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function listFiles(root: string, rel = ''): string[] {
  const out: string[] = []
  const dir = path.join(root, rel)
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = rel ? `${rel}/${entry.name}` : entry.name
    if (entry.isDirectory()) out.push(...listFiles(root, p))
    else if (entry.isFile()) out.push(p)
  }
  return out.sort()
}

// Claude Code's own frontmatter patterns (bundled CLI 2.1.283, `Dk` and `ZH`
// in its frontmatter parser): the block it parses ends at the FIRST `---`,
// even one in the middle of a line; the line-anchored form is what it checks
// that against. A byte-order mark is stripped first.
const FM_CLI = /^---\s*\n([\s\S]*?)---\s*\n?/
const FM_OPEN = /^---\s*\n/
const FM_LINES = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/
// A line at column 0 of accepted frontmatter: blank, a comment, a sequence
// item of the key above it, or a plain `key:` (the CLI's own key alphabet).
// Anything else (a quoted, explicit `?` or merge `<<` key, a flow mapping, an
// anchor, a directive, a document marker) is refused.
const TOP_LEVEL_LINE = /^(?:$|#|-(?:[ \t]|$)|[A-Za-z0-9_][A-Za-z0-9_.-]*:(?:[ \t]|$))/

class FrontmatterError extends Error {}

function refuseForm(reason: string): never {
  throw new FrontmatterError(
    `frontmatter uses a YAML form a plugin package may not use (${reason}); write plain "key: value" lines`,
  )
}

/**
 * The YAML frontmatter's top-level keys, each value as a list of strings (a
 * scalar is one item; anything else is kept as JSON so a tool check refuses
 * it). Undefined when Claude Code would see no frontmatter.
 *
 * Fails closed: the block is cut exactly where Claude Code cuts it, and it
 * must be one block mapping of plain keys at column 0, with no anchors,
 * aliases, tags, merge keys, directives or second document. Those forms are
 * where two YAML parsers (ours and the CLI's) could read different keys, and
 * a key only the CLI sees (`"hooks":`, `? hooks`, `{hooks: …}`, `<<: *x`)
 * would get past the checks below. Anything else throws, and the caller
 * refuses the package.
 */
export function frontmatter(text: string): Map<string, string[]> | undefined {
  const body = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text
  const match = FM_CLI.exec(body)
  if (!match) {
    if (FM_OPEN.test(body)) refuseForm('an opening "---" with no closing "---"')
    return undefined
  }
  const block = match[1] ?? ''
  const lined = FM_LINES.exec(body)?.[1]
  if ((block.trim() !== '' || (lined ?? '').trim() !== '') && lined?.trim() !== block.trim()) {
    refuseForm('a "---" that is not on a line of its own')
  }
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith(' ')) continue
    if (!TOP_LEVEL_LINE.test(line)) refuseForm(`the line ${JSON.stringify(line.slice(0, 40))}`)
  }

  const doc = parseDocument(block, { merge: false, uniqueKeys: true, prettyErrors: false })
  const issue = doc.errors[0] ?? doc.warnings[0]
  if (issue) throw new FrontmatterError(`frontmatter is not valid YAML: ${issue.message.split('\n')[0]}`)
  if (doc.directives?.docStart) refuseForm('a document marker')
  visit(doc, {
    Alias() {
      refuseForm('an alias')
    },
    Node(_key, node) {
      if (node.anchor) refuseForm('an anchor')
      if (node.tag) refuseForm('a tag')
    },
  })
  if (doc.contents === null) return new Map()
  if (!isMap(doc.contents)) throw new FrontmatterError('frontmatter is not a YAML mapping')
  if (doc.contents.flow) refuseForm('a flow mapping')

  const keys = new Map<string, string[]>()
  const item = (v: unknown): string => (typeof v === 'string' ? v : JSON.stringify(v))
  for (const pair of doc.contents.items) {
    const key = pair.key
    if (!isScalar(key) || key.type !== 'PLAIN' || typeof key.value !== 'string') refuseForm('a key that is not plain')
    const at = key.range?.[0] ?? -1
    if (at !== 0 && block[at - 1] !== '\n') refuseForm(`the key "${key.value}" is not at column 0`)
    const value: unknown = isNode(pair.value) ? pair.value.toJS(doc) : pair.value
    keys.set(key.value, value === null || value === undefined ? [] : Array.isArray(value) ? value.map(item) : [item(value)])
  }
  return keys
}

/** Splits a tool list the way the skills page accepts it: space- or comma-separated, or a YAML list. */
export function toolNames(values: readonly string[]): string[] {
  return values
    .flatMap((v) => v.split(/[,\s]+(?![^(]*\))/))
    .map((t) => t.trim().replace(/^['"]|['"]$/g, ''))
    .filter(Boolean)
}

/** MCP tools only; the permission seam gives each its tier. */
export function isAllowlistedTool(name: string): boolean {
  return /^mcp__[A-Za-z0-9_-]+(?:__[A-Za-z0-9_*-]+)?$/.test(name)
}

const INJECTION_INLINE = /(^|\s)!`/m
// Anywhere in the text, as the CLI matches it (2.1.283: /```!\s*\n?([\s\S]*?)\n?```/g).
const INJECTION_BLOCK = /(```|~~~)!/

function checkMarkdown(rel: string, text: string, problems: string[]): void {
  if (INJECTION_INLINE.test(text) || INJECTION_BLOCK.test(text)) {
    problems.push(`${rel}: runs a shell command through dynamic context injection (!\`...\`)`)
  }
  let fm: Map<string, string[]> | undefined
  try {
    fm = frontmatter(text)
  } catch (err) {
    if (!(err instanceof FrontmatterError)) throw err
    problems.push(`${rel}: ${err.message}`)
    return
  }
  if (!fm) return
  for (const key of ['hooks', 'mcpServers', 'permissionMode']) {
    if (fm.has(key)) problems.push(`${rel}: frontmatter "${key}" is not allowed in a plugin package`)
  }
  for (const key of ['allowed-tools', 'tools']) {
    const refused = toolNames(fm.get(key) ?? []).filter((t) => !isAllowlistedTool(t))
    if (refused.length) {
      problems.push(`${rel}: ${key} names tools the harness does not offer or allow: ${refused.join(', ')}`)
    }
  }
}

function hookHandlers(config: unknown): { event: string; handler: unknown }[] {
  const events = isRecord(config) && isRecord(config.hooks) ? config.hooks : config
  if (!isRecord(events)) return []
  const out: { event: string; handler: unknown }[] = []
  for (const [event, groups] of Object.entries(events)) {
    for (const group of Array.isArray(groups) ? groups : [groups]) {
      const handlers = isRecord(group) ? group.hooks : undefined
      for (const handler of Array.isArray(handlers) ? handlers : [handlers]) out.push({ event, handler })
    }
  }
  return out
}

function mcpEntries(map: unknown): [string, unknown][] {
  const servers = isRecord(map) && isRecord(map.mcpServers) ? map.mcpServers : map
  return isRecord(servers) ? Object.entries(servers) : []
}

export function skillNames(root: string, manifest: Record<string, unknown>): string[] {
  const dirs = ['skills', ...(Array.isArray(manifest.skills) ? manifest.skills : [manifest.skills])]
    .filter((d): d is string => typeof d === 'string')
    .map((d) => d.replace(/^\.\/+/, '').replace(/\/+$/, '') || '.')
  const names = new Set<string>()
  for (const dir of dirs) {
    const abs = path.resolve(root, dir)
    if (!isInside(root, abs) || !existsSync(abs) || !statSync(abs).isDirectory()) continue
    if (existsSync(path.join(abs, 'SKILL.md')) && dir !== 'skills') {
      names.add(path.basename(dir === '.' ? root : abs))
      continue
    }
    for (const entry of readdirSync(abs, { withFileTypes: true })) {
      if (entry.isDirectory() && existsSync(path.join(abs, entry.name, 'SKILL.md'))) names.add(entry.name)
    }
  }
  if (names.size === 0 && manifest.skills === undefined && existsSync(path.join(root, 'SKILL.md'))) {
    names.add(path.basename(root))
  }
  return [...names].sort()
}

export function markdownIn(root: string, value: unknown, fallback: string, recursive: boolean): string[] {
  const entries = value === undefined ? [fallback] : Array.isArray(value) ? value : [value]
  const out = new Set<string>()
  for (const entry of entries) {
    if (typeof entry !== 'string') continue
    const rel = entry.replace(/^\.\/+/, '').replace(/\/+$/, '')
    const abs = path.resolve(root, rel)
    if (abs === root || !isInside(root, abs) || !existsSync(abs)) continue
    if (statSync(abs).isFile()) {
      if (rel.endsWith('.md')) out.add(rel.replace(/\.md$/, '').split('/').pop()!)
      continue
    }
    const walk = (dir: string, prefix: string) => {
      for (const e of readdirSync(dir, { withFileTypes: true })) {
        if (e.isFile() && e.name.endsWith('.md')) out.add(`${prefix}${e.name.slice(0, -3)}`)
        else if (e.isDirectory() && recursive) walk(path.join(dir, e.name), `${prefix}${e.name}:`)
      }
    }
    walk(abs, '')
  }
  return [...out].sort()
}

/** Vets the package at `root` (a materialised copy) and builds its review. `fallbackName` is used without a manifest name. */
export function vetPackage(root: string, fallbackName?: string): Vetting {
  const abs = path.resolve(root)
  const problems = [...pluginProblems(abs)]
  const endpoints: Endpoint[] = []
  const declared = declaredConfigs(abs)
  const m = declared.manifest

  // The name namespaces every skill (/<name>:<skill>) and is the row's key.
  const rawName = typeof m.name === 'string' ? m.name : fallbackName
  let name: string | undefined
  if (rawName === undefined) {
    problems.push('the package has no name: add .claude-plugin/plugin.json with a "name"')
  } else if (!PLUGIN_NAME_RE.test(rawName) || rawName.includes('--')) {
    problems.push(
      `plugin name "${rawName}" is not 2–32 lower-case letters, digits and single hyphens, starting with a letter`,
    )
  } else if (RESERVED_PLUGIN_NAMES.has(rawName)) {
    problems.push(`plugin name "${rawName}" is reserved`)
  } else {
    name = rawName
  }

  for (const key of ['dependencies', 'userConfig', 'channels', 'settings', 'workflows']) {
    if (m[key] !== undefined) problems.push(`plugin.json "${key}" is not supported for a plugin package`)
  }
  if (existsSync(path.join(abs, 'settings.json'))) problems.push('settings.json is not supported for a plugin package')
  if (existsSync(path.join(abs, 'workflows'))) problems.push('workflows/ holds JavaScript, which a plugin package may not ship')

  const files = listFiles(abs)
  for (const rel of files.filter((f) => f.toLowerCase().endsWith('.md'))) {
    checkMarkdown(rel, readFileSync(path.join(abs, rel), 'utf8'), problems)
  }
  // Inline commands in the manifest: { name: { content: "..." } }.
  if (isRecord(m.commands)) {
    for (const [cmd, def] of Object.entries(m.commands)) {
      if (isRecord(def) && typeof def.content === 'string') checkMarkdown(`plugin.json commands.${cmd}`, def.content, problems)
    }
  }

  const hooks: ReviewHook[] = []
  for (const { where, config } of declared.hooks) {
    for (const { event, handler } of hookHandlers(config)) {
      const type = isRecord(handler) && typeof handler.type === 'string' ? handler.type : 'command'
      const hook: ReviewHook = { event, type }
      if (!PACKAGE_HOOK_EVENTS.has(event)) {
        problems.push(
          `${where}: a ${event} hook is not allowed in a plugin package; it may decide or rewrite a tool call, or it is not known`,
        )
      }
      if (type === 'mcp_tool') {
        problems.push(`${where}: ${event} has an mcp_tool hook, which calls a tool outside the approval seam`)
      } else if (type === 'http' && isRecord(handler)) {
        const dollars = dollarFields(handler)
        if (dollars.length) {
          problems.push(`${where}: ${event} http hook references a variable ($) in ${dollars.join(', ')}`)
        }
        if (handler.allowedEnvVars !== undefined) problems.push(`${where}: ${event} http hook sets allowedEnvVars`)
        if (typeof handler.url === 'string') {
          hook.url = handler.url
          endpoints.push({ what: `${where} ${event} hook`, url: handler.url })
        } else {
          problems.push(`${where}: ${event} http hook has no url`)
        }
      }
      hooks.push(hook)
    }
  }

  const mcpServers: ReviewMcpServer[] = []
  for (const { where, map } of declared.mcp) {
    for (const [server, config] of mcpEntries(map)) {
      if (!isRecord(config)) continue // pluginProblems reports it
      const dollars = dollarFields(config)
      if (dollars.length) {
        problems.push(
          `${where}: MCP server "${server}" references a variable ($) in ${dollars.join(', ')}, which could expand to a secret`,
        )
      }
      if (config.headersHelper !== undefined) problems.push(`${where}: MCP server "${server}" has a headersHelper command`)
      if (config.type !== NON_COMMAND_MCP_TYPE && config.type !== undefined && config.command === undefined) {
        problems.push(`${where}: MCP server "${server}" is "${String(config.type)}"; only Streamable HTTP ("http") is supported`)
      }
      if (typeof config.url === 'string') {
        mcpServers.push({ name: server, type: String(config.type), url: config.url })
        endpoints.push({ what: `MCP server "${server}"`, url: config.url })
      }
    }
  }

  const review: PackageReview | undefined = name
    ? {
        name,
        description: typeof m.description === 'string' ? m.description : null,
        version: typeof m.version === 'string' ? m.version : null,
        skills: skillNames(abs, m).map((s) => `${name}:${s}`),
        commands: [
          ...markdownIn(abs, isRecord(m.commands) ? [] : m.commands, 'commands', false),
          ...(isRecord(m.commands) ? Object.keys(m.commands) : []),
        ]
          .sort()
          .map((c) => `${name}:${c}`),
        agents: markdownIn(abs, m.agents, 'agents', true).map((a) => `${name}:${a}`),
        hooks,
        mcp_servers: mcpServers,
        files: files.filter((f) => /\.(md|json)$/i.test(f)),
      }
    : undefined

  const unique = [...new Set(problems)]
  return {
    review,
    problems:
      unique.length > MAX_REPORTED_PROBLEMS
        ? [...unique.slice(0, MAX_REPORTED_PROBLEMS), `and ${unique.length - MAX_REPORTED_PROBLEMS} more`]
        : unique,
    endpoints,
  }
}
