import type { ToolAccess } from './model.js'

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
