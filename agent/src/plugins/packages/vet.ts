import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { isMap, isNode, isScalar, parseDocument, visit } from 'yaml'
import { declaredConfigs, hookEvents, isInside, isOutsideProblem, noCommand, pluginProblems } from '../../harness/plugins.js'
import { type CallPlugin, cutName, skillItem, subagentItem } from '../../access/items.js'
import { LISTING_CAPS, storableCut } from '../../access/listings.js'
import { PLUGIN_NAME_RE, RESERVED_PLUGIN_NAMES } from '../registry.js'

// Vetting a plugin PACKAGE (issue #297, "Review before enable"), on top of the
// harness's own rules for any local plugin (src/harness/plugins.ts
// `pluginProblems`: no command hooks, no hooks modules, no stdio/bundled MCP
// servers, no LSP servers, no monitors). A package is someone else's code
// fetched from the network, so it gets these as well, each with its source:
//
//   - Dynamic context injection. A skill or command line starting `` !`cmd` ``,
//     or a ```` ```! ```` block, is run by a shell "before the skill content is
//     sent to Claude" (https://code.claude.com/docs/en/skills, dynamic context
//     injection), in the harness's environment, which holds the Claude
//     credential. Every query sets `disableSkillShellExecution`
//     (harness/options.ts), so the CLI puts a placeholder in place of both
//     forms (measured on CLI 2.1.283 and 2.1.287, test/pluginPackages.e2e.test.ts; before
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
//
// AN ADMIN MAY ALLOW what these rules refuse, for one package at one pin: the
// review lists every refusal (`refused`), and approving with `allow_refused`
// loads the package as it is (store.ts), with the Claude Code built-ins its
// skills and subagents name (`builtin_tools`) and skill shell injection on
// (run.ts `builtinTools`). That is a decision to run the package's code as the agent service itself:
// with the Claude credential in its environment, and able to read the
// service's (the database URL, the key file behind every stored secret). A
// reserved or non-kebab name is allowable too. Never allowable (`fatal`): a
// name that is not a safe path segment (`SAFE_NAME_RE`), a declared file
// outside the package (not part of the pinned hash), and, in install.ts,
// symlinks and submodules, the content hash, and the egress check on every
// URL the package declares.

/**
 * `id` is `hookId`'s: its access row is `hook:<event>:<id>` (#4757). Always set by this
 * vetting; absent only in a review stored before it. `matcher` is its group's, when a string.
 */
export type ReviewHook = { event: string; type: string; url?: string; id?: string; matcher?: string }
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
  /**
   * What the rules refuse, which loads only when an admin approves the pin
   * with `allow_refused` (store.ts). Absent in a review stored before #1540's
   * follow-up, which was refused outright, so empty.
   */
  refused?: string[]
  /**
   * The Claude Code built-ins its skills and subagents name (`allowed-tools`,
   * `tools`). A turn offers them while the pin is allowed (run.ts
   * `builtinTools`). Absent in an older review.
   */
  builtin_tools?: string[]
  /**
   * Each item's id (`skill:<name>` for a skill or a command, `subagent:<name>`) to its
   * frontmatter `description`, cut to `LISTING_CAPS.description` (#4757). A skill and a
   * command of one name are one item: the skill's description, else the command's.
   * Absent in an older review.
   */
  described?: Record<string, string | null>
  /**
   * `PackageItems.aliases`: per kind, a frontmatter `name` to every item declaring it
   * (`subagent:deep-review` → [`subagent:review:deep`]). Claude Code's `subagent_type` is
   * the frontmatter name. Absent in an older review.
   */
  aliases?: Record<string, string[]>
  /** What the caps cut from this review (`skill:x description`, `hooks/hooks.json Stop matcher`). */
  cut?: string[]
}

export type Endpoint = { what: string; url: string }

export type Vetting = {
  /** Undefined when the package has no usable name. */
  review: PackageReview | undefined
  /** Everything the rules refuse, fatal or not, uncapped; a refusal caps it (`capProblems`). */
  problems: string[]
  /** The problems no approval can allow (see the header); a subset of `problems`. */
  fatal: string[]
  endpoints: Endpoint[]
}

const NON_COMMAND_MCP_TYPE = 'http'

/** ScadBuddy's own plugin (plugins/scadbuddy), which harness/ownPlugin.ts loads. */
export const OWN_PLUGIN_NAME = 'scadbuddy'

/** The least a package name must be, approved or not; the stricter rules are allowable. */
export const SAFE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

// The hook events a package may use: an allowlist, so an event added to
// Claude Code later is refused until it is read. Left out on purpose, from
// the CLI 2.1.283 event list (the same 33 events in 2.1.287) and the hooks reference
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

/** `problems` without repeats, capped at MAX_REPORTED_PROBLEMS with an "and N more". */
export function capProblems(problems: readonly string[]): string[] {
  const unique = [...new Set(problems)]
  return unique.length > MAX_REPORTED_PROBLEMS
    ? [...unique.slice(0, MAX_REPORTED_PROBLEMS), `and ${unique.length - MAX_REPORTED_PROBLEMS} more`]
    : unique
}

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

// Claude Code's own frontmatter patterns, FM_CLI and FM_LINES below, as they
// read in its frontmatter parser (bundled CLI 2.1.283, byte-identical in
// 2.1.287; its minified names change from build to build): the block it
// parses ends at the FIRST `---`, even one in the middle of a line; the
// line-anchored form is what it checks that against. A byte-order mark is
// stripped first.
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
// Anywhere in the text, as the CLI matches it (2.1.283 and 2.1.287: /```!\s*\n?([\s\S]*?)\n?```/g).
const INJECTION_BLOCK = /(```|~~~)!/

/** The Claude Code built-in a tool list names (`Bash(git *)` → `Bash`); undefined for anything else. */
export function builtinToolOf(name: string): string | undefined {
  const base = name.replace(/\(.*\)$/, '')
  return /^[A-Z][A-Za-z]*$/.test(base) ? base : undefined
}

function checkMarkdown(rel: string, text: string, problems: string[], builtins: Set<string>): void {
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
    for (const tool of refused) {
      const builtin = builtinToolOf(tool)
      if (builtin) builtins.add(builtin)
    }
    if (refused.length) {
      problems.push(`${rel}: ${key} names tools the harness does not offer or allow: ${refused.join(', ')}`)
    }
  }
}

/**
 * JSON with every object's keys sorted, no whitespace, and values as `JSON.stringify`
 * prints them (an `undefined` member left out, one in an array `null`). Not
 * `gate/canonical.ts`, which refuses numbers: a hook's `timeout` is one.
 */
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((v) => (v === undefined ? 'null' : canonicalJson(v))).join(',')}]`
  if (isRecord(value)) {
    const members = Object.keys(value)
      .sort()
      .filter((k) => value[k] !== undefined)
      .map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`)
    return `{${members.join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/**
 * A hook's id (#4757): the first 16 hex digits of the sha256 of the canonical JSON of
 * `{ event, matcher, handler }`. Two byte-identical hooks share one, so one access covers both.
 */
export function hookId(event: string, matcher: unknown, handler: unknown): string {
  return createHash('sha256').update(canonicalJson({ event, matcher, handler })).digest('hex').slice(0, 16)
}

function hookHandlers(config: unknown): { event: string; matcher: unknown; handler: unknown }[] {
  const { events } = hookEvents(config)
  if (!isRecord(events)) return []
  const out: { event: string; matcher: unknown; handler: unknown }[] = []
  for (const [event, groups] of Object.entries(events)) {
    for (const group of Array.isArray(groups) ? groups : [groups]) {
      const handlers = isRecord(group) ? group.hooks : undefined
      const matcher = isRecord(group) ? group.matcher : undefined
      for (const handler of Array.isArray(handlers) ? handlers : [handlers]) out.push({ event, matcher, handler })
    }
  }
  return out
}

function mcpEntries(map: unknown): [string, unknown][] {
  const servers = isRecord(map) && isRecord(map.mcpServers) ? map.mcpServers : map
  return isRecord(servers) ? Object.entries(servers) : []
}

/**
 * The name of the skill a package root is (#4757): its package name with every character
 * outside `[A-Za-z0-9_-]` as `-`. Claude Code 2.1.310 (SDK 0.3.310's bundled `claude`,
 * function `_7t`, read with `strings -n 20`) names a skills path that is itself a skill by
 * its frontmatter `name`, else by `basename(dir)`, with that replacement. A loaded copy's
 * folder is named after the package (derived.ts), so this is the name Claude Code gives a
 * root skill with no frontmatter `name`, and one key, whatever folder the review was built
 * in; a frontmatter `name` is an alias of it.
 */
export function rootSkillName(packageName: string): string {
  return packageName.replace(/[^a-zA-Z0-9_-]/g, '-')
}

/** The package's name for its root skill: the manifest's, else `fallbackName`, else the folder's. */
function packageNameOf(root: string, manifest: Record<string, unknown>, fallbackName: string | undefined): string {
  return typeof manifest.name === 'string' ? manifest.name : (fallbackName ?? path.basename(root))
}

/**
 * Each skill's name to its directory, relative to `root` (`.` for a package that is one
 * skill, named `rootSkillName`). The root is a skill without being listed only when the
 * manifest's `skills` is falsy and there is no `skills` path at all, as Claude Code 2.1.310
 * loads it (the plugin loader's `if(Se.skills){…}else if(!St&&…)`, `St` being whether
 * `<root>/skills` exists; read as `_7t` above).
 */
function skillDirs(root: string, manifest: Record<string, unknown>, fallbackName?: string): Map<string, string> {
  const rootName = rootSkillName(packageNameOf(root, manifest, fallbackName))
  const dirs = ['skills', ...(Array.isArray(manifest.skills) ? manifest.skills : [manifest.skills])]
    .filter((d): d is string => typeof d === 'string')
    .map((d) => d.replace(/^\.\/+/, '').replace(/\/+$/, '') || '.')
  const out = new Map<string, string>()
  const add = (name: string, dir: string) => {
    if (!out.has(name)) out.set(name, path.relative(root, dir) || '.')
  }
  for (const dir of dirs) {
    const abs = path.resolve(root, dir)
    if (!isInside(root, abs) || !existsSync(abs) || !statSync(abs).isDirectory()) continue
    if (existsSync(path.join(abs, 'SKILL.md')) && dir !== 'skills') {
      add(dir === '.' ? rootName : path.basename(abs), abs)
      continue
    }
    for (const entry of readdirSync(abs, { withFileTypes: true })) {
      if (entry.isDirectory() && existsSync(path.join(abs, entry.name, 'SKILL.md'))) add(entry.name, path.join(abs, entry.name))
    }
  }
  if (!manifest.skills && !existsSync(path.join(root, 'skills')) && existsSync(path.join(root, 'SKILL.md'))) {
    add(rootName, root)
  }
  return out
}

export function skillNames(root: string, manifest: Record<string, unknown>, fallbackName?: string): string[] {
  return [...skillDirs(path.resolve(root), manifest, fallbackName).keys()].sort()
}

/** Each Markdown item's name (a nested one `<dir>:<name>` when `recursive`) to its file, relative to `root`. */
function markdownFiles(root: string, value: unknown, fallback: string, recursive: boolean): Map<string, string> {
  const entries = value === undefined ? [fallback] : Array.isArray(value) ? value : [value]
  const out = new Map<string, string>()
  const add = (name: string, file: string) => {
    if (!out.has(name)) out.set(name, path.relative(root, file))
  }
  for (const entry of entries) {
    if (typeof entry !== 'string') continue
    const rel = entry.replace(/^\.\/+/, '').replace(/\/+$/, '')
    const abs = path.resolve(root, rel)
    if (abs === root || !isInside(root, abs) || !existsSync(abs)) continue
    if (statSync(abs).isFile()) {
      if (rel.endsWith('.md')) add(rel.replace(/\.md$/, '').split('/').pop()!, abs)
      continue
    }
    const walk = (dir: string, prefix: string) => {
      for (const e of readdirSync(dir, { withFileTypes: true })) {
        if (e.isFile() && e.name.endsWith('.md')) add(`${prefix}${e.name.slice(0, -3)}`, path.join(dir, e.name))
        else if (e.isDirectory() && recursive) walk(path.join(dir, e.name), `${prefix}${e.name}:`)
      }
    }
    walk(abs, '')
  }
  return out
}

export function markdownIn(root: string, value: unknown, fallback: string, recursive: boolean): string[] {
  return [...markdownFiles(path.resolve(root), value, fallback, recursive).keys()].sort()
}

/** Where an inline command (`plugin.json` `commands.<name>.content`) lives, in `packageItems`. */
export const inlineCommandAt = (name: string): string => `.claude-plugin/plugin.json#commands.${name}`

/** Each command's name to its file (relative to `root`) or its `inlineCommandAt`. */
function commandFiles(root: string, manifest: Record<string, unknown>): Map<string, string> {
  if (!isRecord(manifest.commands)) return markdownFiles(root, manifest.commands, 'commands', false)
  return new Map(Object.keys(manifest.commands).map((name) => [name, inlineCommandAt(name)]))
}

/** A frontmatter key's one string value; undefined when absent, not one string, or unreadable. */
function frontmatterValue(text: string | undefined, key: string): string | undefined {
  if (text === undefined) return undefined
  try {
    const values = frontmatter(text)?.get(key)
    return values?.length === 1 ? values[0] : undefined
  } catch (err) {
    if (err instanceof FrontmatterError) return undefined // checkMarkdown refuses it
    throw err
  }
}

function readText(root: string, rel: string): string | undefined {
  try {
    return readFileSync(path.join(root, rel), 'utf8')
  } catch {
    return undefined
  }
}

/** What one package may make a review store: `LISTING_CAPS`, as a remote listing (#4757). */
const DESCRIPTION_CAP = LISTING_CAPS.description
const NAME_CAP = LISTING_CAPS.name
const MATCHER_CAP = LISTING_CAPS.title
/** A hook url longer than this is refused (allowable), never stored cut: a cut url is another endpoint. */
export const HOOK_URL_CAP = 2_048

// A name as a review stores it (an alias key, a hook event): the one cut the call rule's
// alias lookup uses too (access/items.ts `offCallItem`).
export { cutName }

/** `text` cut to `max` and made storable; `what` joins `cut` (once) when it was longer. */
function capped(text: string, max: number, what: string, cut: string[]): string {
  if (text.length > max && !cut.includes(what)) cut.push(what)
  return storableCut(text, max)
}

/** A manifest path entry as a path relative to the root (`./hooks/x.json` is `hooks/x.json`); `plugin.json hooks` as it is. */
function configAt(where: string): string {
  return where.startsWith('plugin.json ') ? where : path.posix.normalize(where.replaceAll('\\', '/')).replace(/^(\.\/)+/, '')
}

export type PackageItems = {
  /**
   * Each skill item's name to where it lives, relative to the root, the skill first:
   *   - a skill's directory, or `.` when the package root is the skill (never delete `.`),
   *     keyed `rootSkillName(<package name>)` whatever the folder is called;
   *   - a command's file (`commands/wave.md`), or `inlineCommandAt(<name>)` for a
   *     manifest's inline command; its name is an untrusted key that may hold `.`, `#` or
   *     `/`, so compare with `inlineCommandAt`, never parse it.
   * A command is a skill item: Claude Code invokes a slash command through the Skill tool,
   * measured on Claude Code 2.1.310 (SDK 0.3.310's bundled `claude`, `strings -n 20 … |
   * grep 'invoke it via the Skill tool'`: "If it's a slash command, invoke it via the Skill
   * tool"; frontmatter `disable-model-invocation`: "If true, the model cannot invoke this
   * via the Skill tool; only users can type the slash command"). So a skill and a command
   * of one name are one item.
   */
  skills: Map<string, string[]>
  /** Each subagent's name (`review:deep` for `agents/review/deep.md`) to its file. */
  subagents: Map<string, string>
  /**
   * Each hook handler with the config it is in: `hooks/hooks.json`, a manifest path entry
   * relative to the root (`./hooks/x.json` is `hooks/x.json`), or `plugin.json hooks` for
   * one written inline in the manifest. A repeat keeps its id. `event` is cut to the name cap.
   */
  hooks: { id: string; event: string; where: string }[]
  /**
   * Per kind, each frontmatter `name` that differs from its item's name to every item that
   * declares it: `skill:<declared>` → `skill:<item>`s, `subagent:<declared>` →
   * `subagent:<item>`s. A call naming `<declared>` is refused when the direct item or any
   * target is off. A command's frontmatter `name` is not read: nothing measured shows that
   * Claude Code names a command by it (its file or manifest key names it).
   */
  aliases: Map<string, string[]>
  /**
   * `aliases` keyed by each frontmatter `name` whole, never cut: what the loaded pin says, for
   * the call rule (access/items.ts `offCallItem`), so a name over the cap is still matched.
   */
  declared: Map<string, string[]>
}

/** Where each of the package's items lives (#4757), so a derived copy can leave one out. */
export function packageItems(root: string, manifest: Record<string, unknown>, fallbackName?: string): PackageItems {
  return itemsOf(path.resolve(root), manifest, fallbackName).items
}

/**
 * `packageItems` of the plugin at `root`, read from its files as it is loaded (#4757): the
 * call rule's items and aliases (access/items.ts `CallPlugin`) for a plugin loaded as it is,
 * so a review stored before them cannot leave a name out.
 */
export function itemsOfPlugin(root: string, packageName: string): PackageItems {
  return packageItems(root, declaredConfigs(path.resolve(root)).manifest, packageName)
}

/** A loaded plugin as the call rule sees it (access/items.ts `CallPlugin`): its items, and its aliases by whole frontmatter name. */
export function callPluginOf(name: string, source: string, items: PackageItems): CallPlugin {
  return {
    name,
    source,
    items: [...[...items.skills.keys()].map(skillItem), ...[...items.subagents.keys()].map(subagentItem)],
    // The whole frontmatter names, never a review's cut ones.
    aliases: Object.fromEntries(items.declared),
  }
}

type ItemsRead = {
  items: PackageItems
  /** Item id (`skill:<name>`, `subagent:<name>`) to its description, capped. */
  described: Map<string, string | null>
  cut: string[]
  problems: string[]
}

function itemsOf(root: string, manifest: Record<string, unknown>, fallbackName?: string): ItemsRead {
  const cut: string[] = []
  const problems: string[] = []
  const described = new Map<string, string | null>()
  const aliases = new Map<string, string[]>()
  const declaredAliases = new Map<string, string[]>()
  const describe = (id: string, description: string | undefined) => {
    if (described.get(id)) return
    described.set(id, description === undefined ? null : capped(description, DESCRIPTION_CAP, `${id} description`, cut))
  }
  const alias = (file: string, text: string | undefined, id: string, kind: (n: string) => string) => {
    const declared = frontmatterValue(text, 'name')
    if (declared === undefined || kind(declared) === id) return
    if (declared.length > NAME_CAP) problems.push(`${file}: frontmatter "name" is longer than ${NAME_CAP} characters`)
    const key = kind(capped(declared, NAME_CAP, `${id} name`, cut))
    aliases.set(key, [...(aliases.get(key) ?? []), id])
    declaredAliases.set(kind(declared), [...(declaredAliases.get(kind(declared)) ?? []), id])
  }

  const skills = new Map<string, string[]>()
  for (const [name, dir] of skillDirs(root, manifest, fallbackName)) {
    const file = path.join(dir, 'SKILL.md')
    const text = readText(root, file)
    skills.set(name, [dir])
    describe(skillItem(name), frontmatterValue(text, 'description'))
    alias(file, text, skillItem(name), skillItem)
  }
  const inline = isRecord(manifest.commands) ? manifest.commands : {}
  for (const [name, at] of commandFiles(root, manifest)) {
    const def = inline[name]
    const text = isRecord(def) ? (typeof def.content === 'string' ? def.content : undefined) : readText(root, at)
    const description = isRecord(def) && typeof def.description === 'string' ? def.description : frontmatterValue(text, 'description')
    skills.set(name, [...(skills.get(name) ?? []), at])
    describe(skillItem(name), description)
  }
  const subagents = new Map<string, string>()
  for (const [name, file] of markdownFiles(root, manifest.agents, 'agents', true)) {
    const text = readText(root, file)
    subagents.set(name, file)
    describe(subagentItem(name), frontmatterValue(text, 'description'))
    alias(file, text, subagentItem(name), subagentItem)
  }
  const hooks = declaredConfigs(root).hooks.flatMap(({ where, config }) =>
    hookHandlers(config).map(({ event, matcher, handler }) => ({
      id: hookId(event, matcher, handler),
      event: cutName(event),
      where: configAt(where),
    })),
  )
  return { items: { skills, subagents, hooks, aliases, declared: declaredAliases }, described, cut, problems }
}

/** Vets the package at `root` (a materialised copy) and builds its review. `fallbackName` is used without a manifest name. */
export function vetPackage(root: string, fallbackName?: string): Vetting {
  const abs = path.resolve(root)
  const problems = [...pluginProblems(abs)]
  const fatal = problems.filter(isOutsideProblem)
  const endpoints: Endpoint[] = []
  const declared = declaredConfigs(abs)
  const m = declared.manifest

  // The name namespaces every skill (/<name>:<skill>) and is the row's key.
  const rawName = typeof m.name === 'string' ? m.name : fallbackName
  let name: string | undefined
  if (rawName === undefined) {
    fatal.push('the package has no name: add .claude-plugin/plugin.json with a "name"')
  } else if (!SAFE_NAME_RE.test(rawName)) {
    // The name is a directory of the cache and the row's key: never allowable.
    fatal.push(`plugin name "${rawName}" is not 1–64 letters, digits, ".", "_" and "-", starting with a letter or digit`)
  } else {
    name = rawName
    if (!PLUGIN_NAME_RE.test(rawName) || rawName.includes('--')) {
      problems.push(`plugin name "${rawName}" is not 2–32 lower-case letters, digits and single hyphens, starting with a letter`)
    } else if (RESERVED_PLUGIN_NAMES.has(rawName)) {
      problems.push(`plugin name "${rawName}" is reserved: it may clash with ScadBuddy's own plugin or tools`)
    }
  }
  problems.push(...fatal.filter((f) => !problems.includes(f)))

  for (const key of ['dependencies', 'userConfig', 'channels', 'settings', 'workflows']) {
    if (m[key] !== undefined) problems.push(`plugin.json "${key}" is not supported for a plugin package`)
  }
  if (existsSync(path.join(abs, 'settings.json'))) problems.push('settings.json is not supported for a plugin package')
  if (existsSync(path.join(abs, 'workflows'))) problems.push('workflows/ holds JavaScript, which a plugin package may not ship')

  const files = listFiles(abs)
  const builtins = new Set<string>()
  for (const rel of files.filter((f) => f.toLowerCase().endsWith('.md'))) {
    checkMarkdown(rel, readFileSync(path.join(abs, rel), 'utf8'), problems, builtins)
  }
  // Inline commands in the manifest: { name: { content: "..." } }.
  if (isRecord(m.commands)) {
    for (const [cmd, def] of Object.entries(m.commands)) {
      if (isRecord(def) && typeof def.content === 'string') {
        checkMarkdown(`plugin.json commands.${cmd}`, def.content, problems, builtins)
      }
    }
  }

  const items = itemsOf(abs, m, fallbackName)
  problems.push(...items.problems)
  const cut = [...items.cut]
  const hooks: ReviewHook[] = []
  for (const { where, config } of declared.hooks) {
    for (const { event, matcher, handler } of hookHandlers(config)) {
      const type = isRecord(handler) && typeof handler.type === 'string' ? handler.type : 'command'
      // The id is the hook's own, uncut; what the review shows (and says of it) is capped.
      const shown = cutName(event)
      if (event.length > NAME_CAP) cut.push(`${where} ${shown} event`)
      const hook: ReviewHook = { event: shown, type: capped(type, NAME_CAP, `${where} ${shown} type`, cut), id: hookId(event, matcher, handler) }
      if (typeof matcher === 'string') hook.matcher = capped(matcher, MATCHER_CAP, `${where} ${shown} matcher`, cut)
      if (!PACKAGE_HOOK_EVENTS.has(event)) {
        problems.push(
          `${where}: a ${shown} hook is not allowed in a plugin package; it may decide or rewrite a tool call, or it is not known`,
        )
      }
      if (type === 'mcp_tool') {
        problems.push(`${where}: ${shown} has an mcp_tool hook, which calls a tool outside the approval seam`)
      } else if (type === 'http' && isRecord(handler)) {
        const dollars = dollarFields(handler)
        if (dollars.length) {
          problems.push(`${where}: ${shown} http hook references a variable ($) in ${dollars.join(', ')}`)
        }
        if (handler.allowedEnvVars !== undefined) problems.push(`${where}: ${shown} http hook sets allowedEnvVars`)
        if (typeof handler.url === 'string') {
          if (handler.url.length > HOOK_URL_CAP) {
            // The egress check still sees the whole url; the review keeps none.
            problems.push(`${where}: ${shown} http hook url is longer than ${HOOK_URL_CAP} characters`)
            cut.push(`${where} ${shown} url`)
          } else {
            hook.url = handler.url
          }
          endpoints.push({ what: `${where} ${shown} hook`, url: handler.url })
        } else {
          problems.push(`${where}: ${shown} http hook has no url`)
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
      if (config.type !== NON_COMMAND_MCP_TYPE && config.type !== undefined && noCommand(config)) {
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
        skills: skillNames(abs, m, fallbackName).map((s) => `${name}:${s}`),
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
        builtin_tools: [...builtins].sort(),
        described: Object.fromEntries(items.described),
        aliases: Object.fromEntries(items.items.aliases),
        cut: [...new Set(cut)],
      }
    : undefined

  const unique = [...new Set(problems)]
  const fatalSet = new Set(fatal)
  // Uncapped: an admin allows exactly the list the review shows, never "and N more".
  if (review) review.refused = unique.filter((p) => !fatalSet.has(p))
  return { review, problems: unique, fatal: [...fatalSet], endpoints }
}
