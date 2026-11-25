import type { Filament, Listing, ParamValue, PublicParam } from './api/client'
import { colourPlan } from './colours'
import { customerFilaments } from './params'

/**
 * #6710 §6.2, §6.3: a file listing's parts as the page's colour parts. Each part is an
 * offered slot of the file ("Part 1", "Part 2", …), named `colours.<slot>` as the shop's
 * `listing_colours_clash` names it, so the same form (`ListingView`), the same rule
 * (`colours.ts` `colourPlan`: "choose a colour, the plastic follows") and the same clash
 * text serve it. A part takes any colour on offer; it starts at the listing filament the
 * shop chose for it (`PublicSlot.default`). The page never shows a material, the file's own
 * colours or its name, and a support filament is never offered.
 */

const fold = (text: string) => text.trim().toLowerCase()

/** A part's form name: the key the shop's price, add and clash refusal name the slot by. */
export function partName(slot: number): string {
  return `colours.${slot}`
}

/** The listing as its form shows it: one colour part per offered slot, in slot order. */
export function partsView(listing: Listing): Listing {
  const offered = customerFilaments(listing.colours)
  const choices = [...new Map(offered.map((filament) => [fold(filament.colour), filament.colour])).values()]
  const params: Record<string, PublicParam> = {}
  for (const part of listing.slots ?? []) {
    const start = offered.find((filament) => filament.id === part.default)
    params[partName(part.slot)] = { kind: 'choice', label: part.label, group: '', colour: true, choices, default: start?.colour ?? null }
  }
  return { ...listing, params }
}

/**
 * The listing filament of each part (by slot, as the price and the add take them) for the
 * colours on screen (`values`, the visitor's own picks): the colour each part shows, in the
 * material those colours fix. Null while a part has no colour on offer. With colours of no
 * one material, each part takes its colour's first filament, and the shop refuses them with
 * `listing_colours_clash`. The shop prices what this answers with no pick when a listing is
 * published (`backend/scadbuddy/orders/storefront/listings.py` `page_colours`, #7523): both
 * are tested on `test-fixtures/storefront/file-page-colours.json`.
 */
export function partFilaments(view: Listing, values: Readonly<Record<string, ParamValue>>): Record<string, string> | null {
  const plan = colourPlan(view, values)
  const offered = customerFilaments(view.colours)
  const chosen: Record<string, string> = {}
  for (const part of view.slots ?? []) {
    const colour = plan.shown[partName(part.slot)]
    if (colour === undefined) return null
    const same = offered.filter((filament) => fold(filament.colour) === fold(colour))
    const filament = same.find((f) => plan.material !== null && fold(f.material) === plan.material) ?? same[0]
    if (!filament) return null
    chosen[String(part.slot)] = filament.id
  }
  return chosen
}

/** A filament's colour with its alpha byte, `#RRGGBBAA`, as the viewer recolours a part (opaque spells FF). */
function withAlpha(filament: Filament): string {
  const alpha = Math.max(0, Math.min(255, filament.alpha ?? 255))
  return `${filament.colour.slice(0, 7)}${alpha.toString(16).padStart(2, '0').toUpperCase()}`
}

/** The colour each slot of the preview takes, from the filaments `partFilaments` chose. */
export function slotColours(listing: Listing, chosen: Readonly<Record<string, string>>): Record<number, string> {
  const colours: Record<number, string> = {}
  for (const [slot, id] of Object.entries(chosen)) {
    const filament = listing.colours.find((f) => f.id === id)
    if (filament) colours[Number(slot)] = withAlpha(filament)
  }
  return colours
}
