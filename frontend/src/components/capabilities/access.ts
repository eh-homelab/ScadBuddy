/**
 * The access rules of Settings → Tools & plugins (#2171, spec §4.1), pure: what an item's
 * select offers, what it shows, which choice lowers it below its default (the one
 * confirmation line), a source's summary and the row filter. The declared default is never
 * a floor: every access of the kind is offered unless the agent narrows it (`allowed`).
 */
import { ITEM_ACCESS, type Access, type ItemView, type SourceView } from '../../api/capabilities'

export const ACCESS_RANK: Record<Access, number> = { off: 0, read: 1, on: 1, write: 2, outward: 3 }

const word = (access: Access): string => (access === 'outward' ? 'outward (asks first)' : access)

export function choicesOf(item: ItemView): { value: Access | 'default'; label: string }[] {
  const offered = item.allowed ?? ITEM_ACCESS[item.kind]
  const choices = offered.map((value) => ({
    value: value as Access | 'default',
    label: value === item.default ? `${word(value)} (default)` : word(value),
  }))
  return item.default === null ? [{ value: 'default', label: 'By call (default)' }, ...choices] : choices
}

/** What the item's select shows: a forced access, a stored one that takes effect, else the default. */
export function shownAccess(item: ItemView): Access | 'default' {
  if (item.fixed) return item.fixed.access
  if (item.overridden && item.access !== null) return item.access
  return item.default ?? 'default'
}

/** Choosing `next` ranks below the item's default (a by-call default ranks as outward). */
export function lowers(item: ItemView, next: Access): boolean {
  return ACCESS_RANK[next] < ACCESS_RANK[item.default ?? 'outward'] && next !== shownAccess(item)
}

const isTool = (item: ItemView) => item.kind === 'tool' || item.kind === 'builtin'

export function summarize(source: SourceView): string {
  const { items } = source
  const noun = items.every(isTool) ? 'tool' : 'item'
  if (items.length === 0) return source.kind === 'remote' ? 'No tools listed yet' : 'Nothing listed'
  const count = `${items.length} ${noun}${items.length === 1 ? '' : 's'}`
  const changed = items.filter((i) => i.overridden).length
  return changed === 0 ? count : `${count}, ${changed} changed`
}

/** The row filter: case-insensitive over label, name and description. */
export function matches(item: ItemView, filter: string): boolean {
  const needle = filter.trim().toLowerCase()
  if (needle === '') return true
  return [item.label, item.name, item.description ?? ''].some((text) => text.toLowerCase().includes(needle))
}

/**
 * The one line a lowering says (#2171, spec §4.1), first match. `defaultAccess` `null` is a
 * by-call default, which ranks as outward. Nothing else is said: no checkbox and no extra
 * warning for an outward tool (#4208).
 */
export function lowerLine(defaultAccess: Access | null, from: Access | 'default', to: Access): string | null {
  const asksFirst = defaultAccess === 'outward' || defaultAccess === null
  if (to === 'off') return 'The assistant will not be able to use it.'
  if (to === 'write' && asksFirst && (from === 'read' || from === 'off')) {
    return 'It will be able to change things without asking first.'
  }
  if ((to === 'read' || to === 'write') && asksFirst) return 'It will run without asking first.'
  if (to === 'read' && defaultAccess === 'write') return 'Callers limited to read will be able to use it.'
  return null
}
