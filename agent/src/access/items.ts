import { LISTING_CAPS, storableCut } from './listings.js'
import { type AccessRow, SOURCE_ITEM, type ToolAccess } from './model.js'

// The items of a package or the own plugin (#4757, spec §4.1): a skill, a subagent or a
// hook is switched `on` or `off`; an allowed built-in takes a tool's access. Each is an
// `ai_access` row under its source, its item named by kind. Anything else is a tool.

export type SwitchAccess = 'on' | 'off'
/** What one row holds: a tier or `off` (a tool, a built-in), or `on` (a switched item, a source's `*`). */
export type Access = ToolAccess | 'on'
export type ItemKind = 'tool' | 'hook' | 'skill' | 'subagent' | 'builtin'

export const skillItem = (name: string): string => `skill:${name}`
export const subagentItem = (name: string): string => `subagent:${name}`
export const builtinItem = (name: string): string => `builtin:${name}`
/** `id`: the hook's 16 hex digits. */
export const hookItem = (event: string, id: string): string => `hook:${event}:${id}`

const PREFIXES: readonly [string, Exclude<ItemKind, 'tool'>][] = [
  ['skill:', 'skill'],
  ['subagent:', 'subagent'],
  ['builtin:', 'builtin'],
  ['hook:', 'hook'],
]

/** The kind a name's prefix gives; `itemKindIn` is what a stored row is (its source decides). */
export function itemKindOf(item: string): ItemKind {
  return PREFIXES.find(([prefix]) => item.startsWith(prefix))?.[1] ?? 'tool'
}

/**
 * Whether `source` holds skills, subagents, hooks and built-ins: a package's own source
 * (`package:<p>`, not one of its servers) and the own plugin. Every other source holds only
 * tools, whatever their names (a remote tool may be named `skill:x`).
 */
export function holdsItems(source: string): boolean {
  return source === OWN_PLUGIN_SOURCE || (source.startsWith('package:') && !source.includes('/mcp:'))
}

/** An item's kind in its source: by prefix only where the source holds items, else a tool. */
export function itemKindIn(source: string, item: string): ItemKind {
  return holdsItems(source) ? itemKindOf(item) : 'tool'
}

const OWN_PLUGIN_SOURCE = 'builtin:own-plugin'

/** Least to most strict: across the packages that name one built-in, the strictest wins. */
export const BUILTIN_STRICTNESS: readonly ToolAccess[] = ['read', 'write', 'outward', 'off']

export function strictest(accesses: readonly ToolAccess[]): ToolAccess | undefined {
  let out: ToolAccess | undefined
  for (const access of accesses) {
    if (out === undefined || BUILTIN_STRICTNESS.indexOf(access) > BUILTIN_STRICTNESS.indexOf(out)) out = access
  }
  return out
}

/** `"<plugin>:<name>"`, split at the first `:`; undefined when either part is empty. */
export function pluginOfName(qualified: string): { plugin: string; name: string } | undefined {
  const at = qualified.indexOf(':')
  if (at <= 0 || at === qualified.length - 1) return undefined
  return { plugin: qualified.slice(0, at), name: qualified.slice(at + 1) }
}

/**
 * A name as a package review stores it: a frontmatter `name` (an alias key) or a hook
 * event, cut to `LISTING_CAPS.name` and made storable (plugins/packages/vet.ts re-exports
 * it). A Skill/Agent call's name is cut with it before its alias lookup, so the two agree.
 */
export function cutName(text: string): string {
  return storableCut(text, LISTING_CAPS.name)
}

/**
 * A plugin a turn loads, as the call rule sees it: its name, its `ai_access` source, the
 * items it has (`skill:<name>`, `subagent:<name>`, from the loaded pin) and
 * `PackageItems.aliases`.
 */
export type CallPlugin = {
  name: string
  source: string
  items: readonly string[]
  aliases: Readonly<Record<string, readonly string[]>>
}

/** What a refused call names: the source, and its item (`*` when the whole source is switched off). */
export type OffCallItem = { source: string; item: string }

/** The calls that name a plugin's item: the field that names it, and the item kind. `Task` is Agent's old name. */
const CALLS: Readonly<Record<string, { field: string; kind: 'skill' | 'subagent' }>> = {
  Skill: { field: 'skill', kind: 'skill' },
  Agent: { field: 'subagent_type', kind: 'subagent' },
  Task: { field: 'subagent_type', kind: 'subagent' },
}

/** Whether `tool` is a call `offCallItem` judges. */
export function namesItem(tool: string): boolean {
  return Object.hasOwn(CALLS, tool)
}

/**
 * An agent type as Claude Code 2.1.310 compares it when no type is named exactly: NFKC, lower
 * case, without white space, dashes or underscores. Its Agent tool takes the agent whose
 * `agentType` equals `subagent_type`, else the one agent whose form this gives equals it
 * (`tengu_subagent_type_normalized`); the bundled `claude` of SDK 0.3.310, read with
 * `strings -n 20 claude | grep -o 'function RTe(e){.\{120\}'`:
 * `function RTe(e){return e.normalize("NFKC").toLowerCase().replace(/[\p{White_Space}\p{Pd}_]+/gu,"")}`,
 * imported by the Agent tool's chunk.
 */
export function agentTypeForm(name: string): string {
  return name.normalize('NFKC').toLowerCase().replace(/[\p{White_Space}\p{Pd}_]+/gu, '')
}

/**
 * Whether a call's name `called` reaches the spelling `name`. A skill name as Claude Code
 * 2.1.310's Skill tool takes it: trimmed, one leading `/` dropped (`validateInput({skill:i}){let
 * s=i.trim();…let d=s.startsWith("/");…let r=d?s.substring(1):s`, the same `strings` read),
 * then matched exactly, by name, alias or directory listing name (`bR`, `Bs`). A subagent
 * type as its Agent tool takes it: as it is, exactly, else by `agentTypeForm`.
 */
function reaches(kind: 'skill' | 'subagent', called: string, name: string): boolean {
  return called === name || (kind === 'subagent' && agentTypeForm(called) === agentTypeForm(name))
}

/** A call's name as Claude Code reads it before its lookup (`reaches`). */
function calledName(kind: 'skill' | 'subagent', raw: string): string {
  if (kind === 'subagent') return raw
  const trimmed = raw.trim()
  return trimmed.startsWith('/') ? trimmed.slice(1) : trimmed
}

/**
 * The item a Skill, Agent or Task call names that is off (#4757, spec §4.4), or undefined.
 * The call's name is `input.skill` (Skill) or `input.subagent_type`, read as Claude Code reads
 * it (`calledName`, `reaches`). In each plugin it reaches every item the plugin has whose name,
 * or `<plugin>:<name>`, it names (a nested subagent's name holds a `:` too, so a whole name is
 * looked up in every plugin), and every target of an alias it names the same way (an alias key
 * is cut, `cutName`, so the name is cut before that lookup). Of what it reaches, the first
 * whose plugin is switched off (`*` `off`: `{source, item: '*'}`) or whose row is `off`
 * answers: any one refuses the call (fail closed). A row refuses only an item its plugin has.
 * Pure: agent-durable's `off_call_item` is the same rule, and the `calls` access vectors pin both.
 */
export function offCallItem(tool: string, input: unknown, plugins: readonly CallPlugin[], rows: readonly AccessRow[]): OffCallItem | undefined {
  const call = Object.hasOwn(CALLS, tool) ? CALLS[tool] : undefined
  if (!call || typeof input !== 'object' || input === null || Array.isArray(input)) return undefined
  const raw = (input as Record<string, unknown>)[call.field]
  if (typeof raw !== 'string') return undefined
  const called = calledName(call.kind, raw)
  if (called === '') return undefined
  const prefix = `${call.kind}:`
  const cut = cutName(called)
  const isOff = (source: string, item: string) => rows.some((r) => r.source === source && r.item === item && r.access === 'off')
  for (const plugin of plugins) {
    const named = (name: string, as: string) => reaches(call.kind, as, name) || reaches(call.kind, as, `${plugin.name}:${name}`)
    const direct = plugin.items.filter((id) => id.startsWith(prefix) && named(id.slice(prefix.length), called))
    const aliased = Object.entries(plugin.aliases)
      .filter(([key]) => key.startsWith(prefix) && named(key.slice(prefix.length), cut))
      .flatMap(([, targets]) => targets.filter((t) => plugin.items.includes(t)))
    const reached = [...new Set([...direct, ...aliased])]
    if (reached.length === 0) continue
    if (isOff(plugin.source, SOURCE_ITEM)) return { source: plugin.source, item: SOURCE_ITEM }
    const item = reached.find((id) => isOff(plugin.source, id))
    if (item !== undefined) return { source: plugin.source, item }
  }
  return undefined
}

/**
 * The one sentence a refused call's result says (`offCallItem`'s answer): which item of which
 * plugin is switched off, and that the user can turn it back on. agent-durable says the same
 * (its `off_item_reason`; the `calls` vectors pin the text).
 */
export function offItemReason(off: OffCallItem): string {
  const owner = off.source === OWN_PLUGIN_SOURCE ? "ScadBuddy's own plugin" : `plugin package ${off.source.slice('package:'.length)}`
  const what = off.item === SOURCE_ITEM ? owner : `${itemKindOf(off.item)} "${off.item.slice(off.item.indexOf(':') + 1)}" of ${owner}`
  return `${what} is switched off in Settings → Tools & plugins; the user can turn it back on there`
}
