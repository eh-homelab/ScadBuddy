import type { Listing, PublicParam } from './api/client'

/** A colour part of a listing: its parameter's name and the parameter. */
export interface ColourPart {
  name: string
  param: Extract<PublicParam, { kind: 'choice' }>
}

/** One group of colour parts, in the order the listing gives them. */
export interface ColourSection {
  /** The schema's group, as the shop wrote it ("Floors colours"); '' for parts with none. */
  group: string
  parts: ColourPart[]
}

/**
 * From this many colour parts, a listing whose parts are in two or more groups shows them
 * under their group headings, each group closed but the first (#8127). A shorter list
 * stays one plain list.
 */
export const GROUP_FROM = 7
/** From this many parts, a group offers one colour for all of its parts. */
export const GROUP_ALL_FROM = 2
/** From this many parts, a plain list offers one colour for all of its parts. */
export const LIST_ALL_FROM = 4

/** The colour parts of `listing`, in the listing's order. */
export function colourParts(listing: Listing): ColourPart[] {
  return Object.entries(listing.params).flatMap(([name, param]) =>
    param.kind === 'choice' && param.colour ? [{ name, param }] : [],
  )
}

/**
 * `parts` in their groups: each group where its first part is, its parts in the listing's
 * order. Null when the list is short or has one group: the page shows one plain list then.
 */
export function colourSections(parts: readonly ColourPart[]): ColourSection[] | null {
  const sections = new Map<string, ColourPart[]>()
  for (const part of parts) {
    const group = part.param.group.trim()
    sections.set(group, [...(sections.get(group) ?? []), part])
  }
  if (parts.length < GROUP_FROM || sections.size < 2) return null
  return [...sections.entries()].map(([group, each]) => ({ group, parts: each }))
}

/**
 * The colours `shown` (each part's colour on screen, by part) uses, casefolded, once each,
 * in the parts' order: the colours the popover offers first.
 */
export function usedColours(parts: readonly ColourPart[], shown: Readonly<Record<string, string | undefined>>): string[] {
  const used: string[] = []
  for (const part of parts) {
    const colour = shown[part.name]?.toLowerCase()
    if (colour && !used.includes(colour)) used.push(colour)
  }
  return used
}
