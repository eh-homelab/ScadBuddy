import type { Filament, Listing, ParamValue, PublicParam } from './api/client'
import { closestColour, type Swatch } from './closestColour'
import { customerFilaments, paramFilaments, startColour } from './params'

/**
 * #6258: "choose a colour, the plastic follows". The visitor picks colours only. The page
 * never shows a material. This is the page's copy of the backend's rule
 * (`backend/scadbuddy/orders/storefront/inputs.py` `resolve_choice`). Change both together.
 *
 * - The colours the visitor set fix the material: the first of `listing.materials` (in
 *   order) that offers each of them and a choice of each other colour part. Of those, the
 *   first that also offers each other part's default wins.
 * - A part the visitor did not set keeps its default when that material offers it. Else
 *   it moves to the closest colour of that material among its choices (`closestColour.ts`).
 *   It is never kept as a pick, and the page never sends it.
 * - A colour the visitor set never moves. Colours of no one material: the shop answers 422
 *   `listing_colours_clash`, and the parts the visitor did not set show their defaults.
 *
 * The page's copy is a prediction. The colours a render start answers (`StoreRender.values`)
 * win over it (`ListingView`).
 */

type ColourParam = Extract<PublicParam, { kind: 'choice' }>

const fold = (text: string) => text.trim().toLowerCase()

function isColourParam(param: PublicParam): param is ColourParam {
  return param.kind === 'choice' && param.colour
}

/** One colour a part offers: every filament of one hex, whatever its material. */
export interface ColourSwatch {
  /** The colour, lowercase: the key the backend compares by (casefolded). */
  key: string
  /** The colour as the listing spells it: the value the page sends. */
  colour: string
  /** What the customer reads: the colour's name, never its material. */
  name: string
  /** The first filament of the colour, which draws the swatch. */
  filament: Filament
}

/** The words of the listing's materials, which a name or a finish never shows. */
function materialWords(filaments: readonly Filament[]): RegExp | null {
  const words = [...new Set(filaments.map((filament) => filament.material.trim()).filter(Boolean))]
  if (words.length === 0) return null
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  return new RegExp(`\\b(?:${escaped.join('|')})\\b`, 'gi')
}

/**
 * The swatches of colour part `param`: one per colour, in the listing's order. A swatch is
 * named by its colour's name and finish ("Blue · Matte"), else its hex. Two of one such
 * name are told apart by their brand, else by their hex (#6152). No name or finish shows a
 * material word (#6258).
 */
export function colourSwatches(param: PublicParam, filaments: readonly Filament[]): ColourSwatch[] {
  const offered = paramFilaments(param, filaments)
  const strip = materialWords(filaments)
  const plain = (text: string | null | undefined) =>
    (strip ? (text ?? '').replace(strip, ' ') : (text ?? ''))
      .split(/\s+/)
      .filter(Boolean)
      .join(' ')
  const byKey = new Map<string, Filament>()
  for (const filament of offered) if (!byKey.has(fold(filament.colour))) byKey.set(fold(filament.colour), filament)
  const first = [...byKey.entries()].map(([key, filament]) => {
    const name = plain(filament.colour_name) || filament.colour.toUpperCase()
    const finish = plain(filament.subtype)
    return { key, filament, base: finish && !fold(name).includes(fold(finish)) ? `${name} · ${finish}` : name }
  })
  const count = (keys: string[]) => keys.reduce((seen, k) => seen.set(k, (seen.get(k) ?? 0) + 1), new Map<string, number>())
  const names = count(first.map((each) => fold(each.base)))
  const withBrand = count(first.map((each) => fold(`${each.base}\u0000${each.filament.brand ?? ''}`)))
  return first.map(({ key, filament, base }) => {
    let name = base
    if ((names.get(fold(base)) ?? 0) > 1 && filament.colour_name) {
      const brand = filament.brand?.trim()
      if (brand && (withBrand.get(fold(`${base}\u0000${brand}`)) ?? 0) < 2) name = `${base} (${brand})`
      else name = `${base} ${filament.colour.toUpperCase()}`
    }
    return { key, colour: filament.colour, name, filament }
  })
}

/** A part's label inside a sentence: "the rim colour" for "Rim colour". */
function partName(label: string): string {
  return label.length > 1 && label[1] === label[1]!.toLowerCase() ? label[0]!.toLowerCase() + label.slice(1) : label
}

/** "a", "a and b", "a, b and c". */
function joinAnd(items: readonly string[]): string {
  if (items.length <= 1) return items.join('')
  return `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`
}

/**
 * What a visitor reads when the shop refuses colours that share no material (422
 * `listing_colours_clash`, #6258): the parts it names, by their labels, never a raw error.
 * A part the page does not know (a slot, `colours.0`) is left out.
 */
export function clashText(listing: Listing | undefined, parts: readonly string[]): string {
  const named = parts.flatMap((part) => {
    const param = listing?.params[part]
    return param ? [`the ${partName(param.label)}`] : []
  })
  const list = joinAnd(named)
  const capital = list.charAt(0).toUpperCase() + list.slice(1)
  const start =
    named.length > 1 ? `${capital} can't go together.` : named.length === 1 ? `${capital} can't go with the other colours.` : "Some colours can't go together."
  return `${start} Each print is made from one kind of plastic, and a few colours only come in one kind. Change one of them.`
}

/** A filament as `closestColour` compares it: its hex, name and `alpha`. */
function swatchOf(filament: Filament): Swatch {
  return { hex: filament.colour, name: filament.colour_name, alpha: filament.alpha }
}

export interface ColourPlan {
  /** The material the colours fix, casefolded; null when none fits them. Never shown. */
  material: string | null
  /** The colour each colour part shows: the visitor's, else its default or the moved default. */
  shown: Record<string, string>
}

/** What `values` (only the visitor's own) resolve to: the colour each colour part shows. */
export function colourPlan(listing: Listing, values: Readonly<Record<string, ParamValue>>): ColourPlan {
  const offered = customerFilaments(listing.colours)
  const of = new Map<string, Filament[]>()
  for (const filament of offered) of.set(fold(filament.material), [...(of.get(fold(filament.material)) ?? []), filament])
  // The shop always sends the effective list. Only the back office's preview of a listing
  // that keeps the shop's default sends none (an empty or missing list, as a draft stored
  // before #6258): it takes the materials in the colours' order.
  const listed = (listing.materials as readonly string[] | undefined) ?? []
  const allowed = listed.length > 0 ? listed : offered.map((filament) => filament.material)
  const order = [...new Set(allowed.map(fold))].filter((material) => of.has(material))
  const offers = (material: string, colour: string) => (of.get(material) ?? []).some((f) => fold(f.colour) === fold(colour))

  const given: [string, string][] = []
  const shop: [string, ColourParam, string | undefined][] = []
  for (const [name, param] of Object.entries(listing.params)) {
    if (!isColourParam(param)) continue
    const value = values[name]
    if (typeof value === 'string') given.push([name, value])
    else shop.push([name, param, startColour(param)])
  }
  const fits = (material: string) =>
    given.every(([, colour]) => offers(material, colour)) &&
    shop.every(([, param]) => param.choices.some((choice) => typeof choice === 'string' && offers(material, choice)))
  const keeps = (material: string) => shop.every(([, , start]) => start !== undefined && offers(material, start))
  const material = order.find((each) => fits(each) && keeps(each)) ?? order.find(fits) ?? null

  const shown: Record<string, string> = Object.fromEntries(given)
  for (const [name, param, start] of shop) {
    if (start === undefined) continue
    shown[name] = start
    if (material === null || offers(material, start)) continue
    const choices = new Set(param.choices.filter((c): c is string => typeof c === 'string').map(fold))
    const seen = new Set<string>()
    const candidates = (of.get(material) ?? []).filter((filament) => {
      const key = fold(filament.colour)
      if (seen.has(key) || !choices.has(key)) return false
      seen.add(key)
      return true
    })
    // The target is the default's filament when the page has one: its name and alpha.
    const own = offered.find((filament) => fold(filament.colour) === fold(start))
    const index = closestColour(own ? swatchOf(own) : { hex: start, name: null }, candidates.map(swatchOf))
    if (index !== null) shown[name] = candidates[index]!.colour
  }
  return { material, shown }
}
