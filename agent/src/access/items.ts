import { LISTING_CAPS, storableCut } from './listings.js'
import type { AccessRow, ToolAccess } from './model.js'

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

/** A plugin a turn loads, as the call rule sees it: its name, its `ai_access` source and `PackageItems.aliases`. */
export type CallPlugin = { name: string; source: string; aliases: Readonly<Record<string, readonly string[]>> }

/** The calls that name a plugin's item: the field that names it, and the item kind. `Task` is Agent's old name. */
const CALLS: Readonly<Record<string, { field: string; item: (name: string) => string }>> = {
  Skill: { field: 'skill', item: skillItem },
  Agent: { field: 'subagent_type', item: subagentItem },
  Task: { field: 'subagent_type', item: subagentItem },
}

/** Whether `tool` is a call `offCallItem` judges. */
export function namesItem(tool: string): boolean {
  return Object.hasOwn(CALLS, tool)
}

/**
 * The item a Skill, Agent or Task call names that is `off` in `rows` (#4757, spec §4.4), or
 * undefined. The call's name is `input.skill` (Skill) or `input.subagent_type`. A qualified
 * name (`<plugin>:<name>`, split at the first `:`) is looked up in that plugin; the whole
 * name is also looked up in every plugin, as an unqualified one is (a nested subagent's name
 * holds a `:` too). In each plugin a name reaches its direct item and every target of that
 * plugin's `aliases` under the item's id with the name cut (`cutName`), and the first of
 * those whose row is `off` answers: any one off refuses the call (fail closed). Pure:
 * agent-durable's `off_call_item` is the same rule, and the `calls` access vectors pin both.
 */
export function offCallItem(tool: string, input: unknown, plugins: readonly CallPlugin[], rows: readonly AccessRow[]): string | undefined {
  const call = Object.hasOwn(CALLS, tool) ? CALLS[tool] : undefined
  if (!call || typeof input !== 'object' || input === null || Array.isArray(input)) return undefined
  const name = (input as Record<string, unknown>)[call.field]
  if (typeof name !== 'string' || name === '') return undefined
  const qualified = pluginOfName(name)
  const lookups: [CallPlugin, string][] = [
    ...(qualified ? plugins.filter((p) => p.name === qualified.plugin).map((p): [CallPlugin, string] => [p, qualified.name]) : []),
    ...plugins.map((p): [CallPlugin, string] => [p, name]),
  ]
  for (const [plugin, item] of lookups) {
    const aliasKey = call.item(cutName(item))
    const targets = Object.hasOwn(plugin.aliases, aliasKey) ? (plugin.aliases[aliasKey] ?? []) : []
    for (const id of [call.item(item), ...targets]) {
      if (rows.some((r) => r.source === plugin.source && r.item === id && r.access === 'off')) return id
    }
  }
  return undefined
}
