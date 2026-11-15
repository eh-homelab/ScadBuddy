import { useId, useState } from 'react'
import { Link } from 'react-router'
import type { CustomizerSchema, Filament, ListingBound, MediaView, ModelSummary, ModelVersion, Param, ParamValue } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { colourProblems, effectiveMaterials, emptyDraft, fixColour, isSupport, isSupportOnly, lockedValue, materialKey, MAX_TEXT_LENGTH, nearestFilaments, offerable, priceBookMaterials, schemaBound, shownParams, titleColours, withoutWords, type ListingDraft, type ListingRefusal } from './listingDraft'
import { customerFilaments } from '../../storefront/params'
import { MaterialOrder } from './MaterialOrder'
import { ListingPictures, type LivePictures, type MediaState } from './ListingPictures'

/**
 * #5282 (spec 2026-11-02 §5.3), #6131 (design 2026-11-13 §5) — one storefront listing in
 * the Storefront page's editor, in the order a shop thinks: the design it sells (one line,
 * "Change" opens the template and version), the product (title, description, pictures),
 * the settings (every parameter in the design's order: an offered one's bounds, a locked
 * one's value; its switch changes the row in place, #6290), the materials allowed and the
 * colours on offer, grouped by material (#6258). A colour that publish would refuse is named first, with a filament
 * to use instead. Controlled: the page holds the draft, loads the schema, the price book
 * and the media, and saves.
 */
export interface ListingEditorProps {
  draft: ListingDraft
  onChange: (draft: ListingDraft) => void
  models: readonly ModelSummary[]
  versions: readonly ModelVersion[] | undefined
  schema: CustomizerSchema | undefined
  filaments: readonly Filament[]
  media: readonly MediaView[]
  /** #6213 — the pictures the public site shows now (`LivePictures`). */
  live?: LivePictures
  /** #6213 — whether `media` has been read yet. */
  mediaState?: MediaState
  /**
   * #6213 — a picture uploaded in the editor: the template as it left it and the new
   * image's id, which the page adds to the listing as it is when the upload ends.
   */
  onPictureUploaded?: (model: ModelSummary, id: string) => void
  /**
   * #6292 review — the listing has loaded: the price book answered and a new listing's smart
   * defaults are in. "Colours on offer" decides then whether it starts open. Default true.
   */
  coloursSettled?: boolean
  /** While `schema` is undefined: whether its read is still running or failed. Default loading. */
  schemaState?: 'loading' | 'failed'
  /** The price book's read, alone (#6292 review): what "Colours on offer" says while it has no filaments. Default ready. */
  bookState?: 'loading' | 'failed' | 'ready'
  /** #6258 — the shop's default materials (`storefront_materials`); undefined until read. */
  shopMaterials?: readonly string[]
  /** #6258 — what the last publish refused about the materials, said beside the control it concerns. */
  refusal?: ListingRefusal | null
}

/** Where the price book's filaments are added (Settings → Printing → Orders). */
export const PRICE_BOOK_PATH = '/settings/printing#orders'

function shown(value: ParamValue | undefined): string {
  if (value === undefined) return 'its default'
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  return String(value)
}

/** What a locked value typed into its field is, for a number or free text parameter. */
function parsed(param: Param, text: string): ParamValue {
  if (param.type === 'number' || param.type === 'integer' || param.type === 'slider') {
    const number = Number(text)
    return text.trim() === '' || Number.isNaN(number) ? text : number
  }
  return text
}

function numberOr(text: string, fallback: number): number {
  const number = Number(text)
  return text.trim() === '' || Number.isNaN(number) ? fallback : number
}

function filamentName(filament: Filament): string {
  return `${filament.material} ${filament.colour_name ?? filament.colour}`
}

function Swatch({ colour }: { colour: string }) {
  return <span aria-hidden="true" className="inline-block size-3 shrink-0 rounded-sm border" style={{ background: colour }} />
}

const BOUND_WORDS = { min: 'From', max: 'To', step: 'Step' } as const

function BoundFields({ param, bound, onChange }: { param: Param; bound: ListingBound; onChange: (bound: ListingBound) => void }) {
  const id = useId()
  const label = param.caption || param.name
  if (param.type === 'color') {
    return <p className="text-xs text-muted-foreground">The customer picks one of the colours on offer below.</p>
  }
  if (bound.kind === 'number') {
    return (
      <div className="grid grid-cols-3 gap-2">
        {(['min', 'max', 'step'] as const).map((key) => (
          <div key={key} className="flex flex-col gap-1">
            <Label htmlFor={`${id}-${key}`} className="text-xs font-normal">
              {BOUND_WORDS[key]}
            </Label>
            <Input
              id={`${id}-${key}`}
              type="number"
              aria-label={`${BOUND_WORDS[key]} ${label}`}
              value={bound[key] ?? ''}
              min={param.min ?? undefined}
              max={param.max ?? undefined}
              step={param.step ?? 'any'}
              onChange={(event) => {
                const text = event.target.value
                if (key === 'step') onChange({ ...bound, step: text.trim() === '' ? null : numberOr(text, bound.step ?? 1) })
                else onChange({ ...bound, [key]: numberOr(text, bound[key]) })
              }}
            />
          </div>
        ))}
      </div>
    )
  }
  if (bound.kind === 'text') {
    return (
      <div className="flex max-w-40 flex-col gap-1">
        <Label htmlFor={id} className="text-xs font-normal">
          Most characters
        </Label>
        <Input
          id={id}
          type="number"
          min={1}
          max={Math.min(param.max_length ?? MAX_TEXT_LENGTH, MAX_TEXT_LENGTH)}
          value={bound.max_length}
          onChange={(event) => onChange({ ...bound, max_length: numberOr(event.target.value, bound.max_length) })}
        />
      </div>
    )
  }
  const all = schemaBound(param)
  const options = all.kind === 'choice' ? all.choices : bound.choices
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1">
      {options.map((choice) => {
        const checked = bound.choices.includes(choice)
        const name = (param.options ?? []).find((option) => option.value === choice)?.name ?? shown(choice)
        return (
          <li key={String(choice)}>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={checked}
                aria-label={`Offer ${name}`}
                onCheckedChange={(on) =>
                  onChange({ ...bound, choices: on === true ? options.filter((c) => c === choice || bound.choices.includes(c)) : bound.choices.filter((c) => c !== choice) })
                }
              />
              {name}
            </label>
          </li>
        )
      })}
    </ul>
  )
}

/**
 * The field a locked value is set in: a switch for a boolean, the options for a select
 * (each kept as its option's own value, so a numeric select stays a number), a swatch and
 * the listing's filaments for a colour, else text.
 */
function LockedField({
  id,
  param,
  label,
  value,
  filaments,
  onChange,
}: {
  id: string
  param: Param
  label: string
  value: ParamValue | undefined
  filaments: readonly Filament[]
  onChange: (value: ParamValue) => void
}) {
  if (param.type === 'boolean') {
    return <Switch id={id} aria-label={`Value of ${label}`} checked={value === true} onCheckedChange={(on) => onChange(on)} />
  }
  const options =
    param.type === 'select'
      ? (param.options ?? []).map((option) => ({ name: option.name || String(option.value), value: option.value }))
      : param.type === 'color'
        ? filaments.map((filament) => ({ name: filamentName(filament), value: filament.colour }))
        : []
  if (options.length > 0) {
    const at = options.findIndex((option) => (param.type === 'color' ? String(option.value).toLowerCase() === String(value).toLowerCase() : option.value === value))
    return (
      <Select
        value={at < 0 ? undefined : String(at)}
        onValueChange={(index) => {
          const option = options[Number(index)]
          if (option) onChange(option.value)
        }}
      >
        <SelectTrigger id={id} aria-label={`Value of ${label}`} className="w-full">
          <SelectValue placeholder={param.type === 'color' && typeof value === 'string' ? value : 'Choose'} />
        </SelectTrigger>
        <SelectContent>
          {options.map((option, index) => (
            <SelectItem key={index} value={String(index)}>
              {param.type === 'color' && <Swatch colour={String(option.value)} />}
              {option.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  }
  return <Input id={id} aria-label={`Value of ${label}`} value={value === undefined ? '' : String(value)} onChange={(event) => onChange(parsed(param, event.target.value))} />
}

/** Why the customer cannot be given `param`, or null. */
function whyNotOffered(param: Param, draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): string | null {
  if (isSupport(param)) return 'Support settings are yours: a customer never sees them.'
  if (param.type === 'font') return 'A customer cannot choose a typeface.'
  if (!offerable(param)) return 'A customer cannot upload a file.'
  if (param.type === 'color' && !filaments.some((filament) => draft.colours.includes(filament.id) && !isSupportOnly(filament, draft, schema, filaments))) {
    return 'Tick a colour on offer below first.'
  }
  return null
}

function ParamRow({ param, draft, schema, filaments, onChange }: { param: Param; draft: ListingDraft; schema: CustomizerSchema | undefined; filaments: readonly Filament[]; onChange: (draft: ListingDraft) => void }) {
  const id = useId()
  const label = param.caption || param.name
  const bound = draft.params[param.name]
  const value = lockedValue(draft, param)
  const why = bound ? null : whyNotOffered(param, draft, schema, filaments)
  // #6152: a label that says "support" or "interface" makes a support setting, which the
  // saved body locks (`listingInput`), as publish refuses to offer it.
  const supportLabel = bound !== undefined && !isSupport(param) && isSupport(param, bound.label)

  function lock() {
    const { [param.name]: _dropped, ...params } = draft.params
    onChange({ ...draft, params, fixed: value === undefined ? draft.fixed : { ...draft.fixed, [param.name]: value } })
  }

  function offer() {
    const { [param.name]: _dropped, ...fixed } = draft.fixed
    onChange({ ...draft, fixed, params: { ...draft.params, [param.name]: schemaBound(param) } })
  }

  return (
    <li className="flex flex-col gap-2 rounded-md border p-3" role="group" aria-labelledby={`${id}-name`} data-testid="listing-param">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span id={`${id}-name`} className="text-sm font-medium" title={param.name}>
          {label}
        </span>
        <label className="flex items-center gap-2 text-xs text-muted-foreground has-[:disabled]:opacity-60">
          <Switch
            checked={bound !== undefined}
            disabled={why !== null}
            aria-label={`Customer can change ${label}`}
            onCheckedChange={(on) => (on ? offer() : lock())}
          />
          Customer can change
        </label>
      </div>
      {bound ? (
        <BoundFields param={param} bound={bound} onChange={(next) => onChange({ ...draft, params: { ...draft.params, [param.name]: next } })} />
      ) : (
        <LockedField
          id={`${id}-value`}
          param={param}
          label={label}
          value={value}
          filaments={filaments.filter((filament) => draft.colours.includes(filament.id))}
          onChange={(next) => onChange({ ...draft, fixed: { ...draft.fixed, [param.name]: next } })}
        />
      )}
      {why && <p className="text-xs text-muted-foreground">{why}</p>}
      {supportLabel && (
        <p className="text-xs text-destructive">A label with “support” or “interface” makes this a support setting: a customer never sees it. Change the label.</p>
      )}
    </li>
  )
}

/**
 * The colours publish would refuse, each with the price book's filaments nearest it first:
 * choosing one adds it to the listing and locks the colour at it.
 */
function ColourProblems({ draft, schema, filaments, onChange }: { draft: ListingDraft; schema: CustomizerSchema | undefined; filaments: readonly Filament[]; onChange: (draft: ListingDraft) => void }) {
  const problems = colourProblems(draft, schema, filaments)
  if (problems.length === 0) return null
  return (
    <section aria-label="Colours to fix" className="flex flex-col gap-2 rounded-md border border-warn/50 bg-warn/5 p-3" data-testid="colour-problems">
      <h3 className="text-sm font-medium">{problems.length === 1 ? 'One colour has no filament' : `${problems.length} colours have no filament`}</h3>
      <p className="text-xs text-muted-foreground">
        The store sells only colours you have as filament. Use a filament instead, or{' '}
        <Link to={PRICE_BOOK_PATH} className="underline">
          add one to the price book
        </Link>
        .
      </p>
      <ul className="flex flex-col gap-2">
        {problems.map((problem) => (
          <li key={problem.name} className="flex flex-wrap items-center gap-2 text-sm">
            <Swatch colour={problem.colour} />
            <span>
              {problem.label} is {problem.colour}
            </span>
            {filaments.length > 0 && (
              <Select
                onValueChange={(filamentId) => {
                  const filament = filaments.find((f) => f.id === filamentId)
                  if (filament) onChange(fixColour(draft, problem.name, filament))
                }}
              >
                <SelectTrigger aria-label={`Filament for ${problem.label}`} className="w-56">
                  <SelectValue placeholder="Use a filament" />
                </SelectTrigger>
                <SelectContent>
                  {nearestFilaments(problem.colour, filaments).map((filament) => (
                    <SelectItem key={filament.id} value={filament.id}>
                      <Swatch colour={filament.colour} />
                      {filamentName(filament)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

/** The design line: the template and its version, with "Change" opening both pickers. */
function DesignLine({ draft, onChange, models, versions }: Pick<ListingEditorProps, 'draft' | 'onChange' | 'models' | 'versions'>) {
  const id = useId()
  const [changing, setChanging] = useState(false)
  const model = models.find((m) => m.slug === draft.slug)
  const version = versions?.find((v) => v.commit === draft.commit)
  if (!changing && model && draft.commit) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="listing-design">
        <span className="text-muted-foreground">Design</span>
        <span className="font-medium">{model.name}</span>
        <span className="text-muted-foreground">
          version {version?.short ?? draft.commit.slice(0, 7)}
          {version?.current ? ' (current)' : ''}
        </span>
        <Button variant="link" size="sm" className="h-auto p-0" onClick={() => setChanging(true)}>
          Change
        </Button>
      </div>
    )
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-model`}>Design</Label>
        <Select value={draft.slug || undefined} onValueChange={(slug) => onChange({ ...emptyDraft(slug), title: draft.title, description: draft.description, colours: draft.colours })}>
          <SelectTrigger id={`${id}-model`} className="w-full">
            <SelectValue placeholder="Choose a design" />
          </SelectTrigger>
          <SelectContent>
            {models.map((m) => (
              <SelectItem key={m.slug} value={m.slug}>
                {m.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-commit`}>Version</Label>
        <Select value={draft.commit || undefined} disabled={!versions?.length} onValueChange={(commit) => onChange({ ...draft, commit })}>
          <SelectTrigger id={`${id}-commit`} className="w-full">
            <SelectValue placeholder={versions && versions.length === 0 ? 'No history to pin' : 'Choose a version'} />
          </SelectTrigger>
          <SelectContent>
            {(versions ?? []).map((v) => (
              <SelectItem key={v.commit} value={v.commit}>
                {v.short} · {v.message}
                {v.current ? ' (current)' : ''}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {changing && model && draft.commit && (
        <Button variant="link" size="sm" className="h-auto justify-self-start p-0" onClick={() => setChanging(false)}>
          Done
        </Button>
      )}
    </div>
  )
}

/** Where the shop's default materials are set (Settings → Printing → Storefront pricing). */
export const SHOP_MATERIALS_PATH = '/settings/printing#storefront-pricing'

/**
 * #6258 — "Materials allowed": the materials a print of this listing may be made of, first
 * preferred. A print is always of one material: the first on this list that has every colour
 * the customer picks (backend `inputs.py` `resolve_choice`). Empty is the shop's default
 * (`storefront_materials`), shown as such until the person chooses the listing's own.
 */
function MaterialsAllowed({
  draft,
  shop,
  filaments,
  refusal,
  onChange,
}: {
  draft: ListingDraft
  shop: readonly string[] | undefined
  filaments: readonly Filament[]
  refusal: ListingRefusal | null
  onChange: (draft: ListingDraft) => void
}) {
  const own = draft.materials.length > 0
  const refused = refusal?.kind === 'material_not_allowed' ? refusal : null
  const refusedFilament = refused ? filaments.find((filament) => filament.id === refused.filament) : undefined
  return (
    <section className="flex flex-col gap-2" role="group" aria-label="Materials allowed">
      <h3 className="text-sm font-semibold">Materials allowed</h3>
      <p className="text-xs text-muted-foreground">
        A print never mixes materials. When a colour comes in more than one, the first material on this list is used. Customers never see the material.
      </p>
      {own ? (
        <>
          <MaterialOrder label="Materials allowed, first preferred" materials={draft.materials} choices={priceBookMaterials(filaments)} onChange={(materials) => onChange({ ...draft, materials })} />
          <Button variant="link" size="sm" className="h-auto self-start p-0" onClick={() => onChange({ ...draft, materials: [] })}>
            Use the shop default
          </Button>
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-dashed px-2.5 py-2 text-sm">
          <span>{shop ? `Shop default (${shop.join(', ')})` : 'Shop default'}</span>
          <Button variant="outline" size="sm" disabled={!shop} onClick={() => shop && onChange({ ...draft, materials: [...shop] })}>
            Choose for this listing
          </Button>
          <Link to={SHOP_MATERIALS_PATH} className="text-xs text-muted-foreground underline">
            Change the shop default
          </Link>
        </div>
      )}
      {refused && (
        <p role="alert" className="text-xs text-destructive">
          {refusedFilament
            ? `Not published: ${filamentName(refusedFilament)} is ${refusedFilament.material}, which this listing does not allow. Allow ${refusedFilament.material}, or untick ${filamentName(refusedFilament)} under Colours on offer.`
            : `Not published: ${refused.detail}.`}
        </p>
      )}
    </section>
  )
}

/** Why publish found no material to print `refusal`'s listing in, said beside the colours. */
function unprintableText(refusal: Extract<ListingRefusal, { kind: 'locked_colours_unprintable' }>, schema: CustomizerSchema | undefined): string {
  const captions = refusal.names.map((name) => schema?.parameters?.find((param) => param.name === name)?.caption || name)
  if (captions.length === 0) {
    return 'Not published: no allowed material has a colour for each setting the customer picks. Tick colours of one material for each of them.'
  }
  const list = captions.join(' and ')
  return `Not published: no allowed material has the locked colour${captions.length > 1 ? 's' : ''} of ${list} and a colour for each setting the customer picks. Tick those colours in one material, or let the customer change ${list}.`
}

/** One filament's tick in "Colours on offer". */
function FilamentTick({ filament, draft, schema, filaments, offersColour, full = false, onTick }: { filament: Filament; full?: boolean; draft: ListingDraft; schema: CustomizerSchema | undefined; filaments: readonly Filament[]; offersColour: boolean; onTick: (on: boolean) => void }) {
  const name = filamentName(filament)
  return (
    <li className="min-w-0">
      <label className="flex min-w-0 items-center gap-2 text-sm">
        <Checkbox checked={draft.colours.includes(filament.id)} aria-label={name} onCheckedChange={(on) => onTick(on === true)} />
        <Swatch colour={filament.colour} />
        <span className="truncate">{full ? name : (filament.colour_name ?? filament.colour)}</span>
        {offersColour && isSupportOnly(filament, draft, schema, filaments) && <span className="text-xs text-muted-foreground">(for supports only, not offered)</span>}
      </label>
    </li>
  )
}

/**
 * #6290 — the listing's filaments, after the settings: a long list (a price book holds
 * dozens), so folded to a count and the ticked names until asked for. #6258: grouped by
 * allowed material, in the list's order, each with "Offer all" and "Offer none"; a ticked
 * filament of a material the listing does not allow is named apart, as publish refuses it
 * (`listing_material_not_allowed`), and support filaments are listed last. Whether it is open is
 * decided once per listing opened, when the listing has loaded (`settled`: the price book
 * has answered and a new listing's smart defaults are in): open when no colour is ticked,
 * folded otherwise. From then on it stays as the person leaves it, so it never folds while
 * they tick colours (#6292 review). The page keys the editor by the opening, so another
 * listing decides again. Until it is settled it follows the ticks; a tick or a Show/Hide
 * before then is the person's choice and is kept.
 */
function ColoursOnOffer({
  draft,
  schema,
  filaments,
  offersColour,
  settled,
  bookState,
  materials,
  refusal,
  onChange,
}: {
  draft: ListingDraft
  schema: CustomizerSchema | undefined
  filaments: readonly Filament[]
  offersColour: boolean
  settled: boolean
  bookState: 'loading' | 'failed' | 'ready'
  /** The allowed materials, first preferred (`effectiveMaterials`); null while the shop's default is not read: every material is listed. */
  materials: readonly string[] | null
  refusal: ListingRefusal | null
  onChange: (draft: ListingDraft) => void
}) {
  const id = useId()
  const ticked = filaments.filter((filament) => draft.colours.includes(filament.id))
  const [chosen, setChosen] = useState<boolean | null>(null)
  const open = chosen ?? ticked.length === 0
  // The decision on load, made once: later ticks never change it.
  if (settled && chosen === null) setChosen(ticked.length === 0)

  const customer = customerFilaments(filaments)
  const support = filaments.filter((filament) => !customer.includes(filament))
  const groups = (materials ?? priceBookMaterials(filaments)).map((material) => ({
    material,
    filaments: customer.filter((filament) => materialKey(filament.material) === materialKey(material)),
  }))
  const allowed = new Set(groups.map((group) => materialKey(group.material)))
  const outside = customer.filter((filament) => draft.colours.includes(filament.id) && !allowed.has(materialKey(filament.material)))
  const outsideMaterials = [...new Map(outside.map((filament) => [materialKey(filament.material), filament.material.trim()])).values()]
  const listed = groups.reduce((count, group) => count + group.filaments.length, 0) + support.length

  function tick(filament: Filament, on: boolean) {
    setChosen(true)
    onChange({ ...draft, colours: on ? [...draft.colours, filament.id] : draft.colours.filter((c) => c !== filament.id) })
  }
  function tickAll(of: readonly Filament[], on: boolean) {
    setChosen(true)
    const ids = new Set(of.map((filament) => filament.id))
    const rest = draft.colours.filter((c) => !ids.has(c))
    onChange({ ...draft, colours: on ? [...rest, ...of.map((filament) => filament.id)] : rest })
  }
  const tickProps = { draft, schema, filaments, offersColour }

  return (
    <section className="flex flex-col gap-1" role="group" aria-label="Colours on offer">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Colours on offer</h3>
        {listed > 0 && (
          <Button variant="link" size="sm" className="h-auto p-0" aria-expanded={open} aria-controls={`${id}-filaments`} onClick={() => setChosen(!open)}>
            {open ? 'Hide the filaments' : `Show all ${listed} filaments`}
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {offersColour ? 'The filaments a customer can pick from, by allowed material' : 'The filaments this product is printed in, by allowed material'}
        {open ? '. Untick a colour to hide it from customers.' : ticked.length === 0 ? ': none ticked yet.' : `: ${ticked.length} ticked (${ticked.map((filament) => (offersColour && isSupportOnly(filament, draft, schema, filaments) ? `${filamentName(filament)}, for supports only` : filamentName(filament))).join('; ')}).`}
      </p>
      {refusal?.kind === 'locked_colours_unprintable' && (
        <p role="alert" className="text-xs text-destructive">
          {unprintableText(refusal, schema)}
        </p>
      )}
      {materials && outside.length > 0 && (
        <div className="flex flex-col gap-1.5 rounded-md border border-warn/50 bg-warn/5 p-2.5 text-xs" data-testid="materials-not-allowed">
          <p>
            Ticked, but not of an allowed material: {outside.map(filamentName).join('; ')}. Publish refuses {outside.length === 1 ? 'it' : 'them'}.
          </p>
          <div className="flex flex-wrap gap-2">
            {outsideMaterials.map((material) => (
              <Button key={materialKey(material)} variant="outline" size="xs" onClick={() => onChange({ ...draft, materials: [...materials, material] })}>
                Allow {material}
              </Button>
            ))}
            <Button variant="outline" size="xs" onClick={() => tickAll(outside, false)}>
              {outside.length === 1 ? 'Untick it' : 'Untick them'}
            </Button>
          </div>
        </div>
      )}
      {filaments.length === 0 && bookState === 'loading' ? (
        <p className="text-xs text-muted-foreground">Reading the price book…</p>
      ) : filaments.length === 0 && bookState === 'failed' ? (
        <p className="text-xs text-warn">The price book could not be read, so no filament can be ticked. Reload the page to try again.</p>
      ) : filaments.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          The price book has no filaments yet.{' '}
          <Link to={PRICE_BOOK_PATH} className="underline">
            Add one
          </Link>
          .
        </p>
      ) : (
        open && (
          <div id={`${id}-filaments`} className="mt-1 flex flex-col">
            {groups.map(({ material, filaments: own }) => {
              const offered = own.filter((filament) => draft.colours.includes(filament.id)).length
              return (
                <div key={materialKey(material)} role="group" aria-label={`${material} colours`} data-testid="material-colours" className="flex flex-col gap-1.5 border-t py-2.5">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <span className="text-sm font-medium">{material}</span>
                    <span className="text-xs text-muted-foreground">
                      {offered} of {own.length} offered
                    </span>
                    {own.length > 0 && (
                      <span className="ml-auto flex gap-3">
                        <Button variant="link" size="sm" className="h-auto p-0 text-xs" disabled={offered === own.length} onClick={() => tickAll(own, true)}>
                          Offer all
                        </Button>
                        <Button variant="link" size="sm" className="h-auto p-0 text-xs" disabled={offered === 0} onClick={() => tickAll(own, false)}>
                          Offer none
                        </Button>
                      </span>
                    )}
                  </div>
                  {own.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      The price book has no {material} filament.{' '}
                      <Link to={PRICE_BOOK_PATH} className="underline">
                        Add one
                      </Link>
                      .
                    </p>
                  ) : (
                    <ul className="grid grid-cols-[repeat(auto-fill,minmax(10rem,1fr))] gap-x-3 gap-y-1">
                      {own.map((filament) => (
                        <FilamentTick key={filament.id} filament={filament} {...tickProps} onTick={(on) => tick(filament, on)} />
                      ))}
                    </ul>
                  )}
                </div>
              )
            })}
            {support.length > 0 && (
              <div role="group" aria-label="Support colours" data-testid="support-colours" className="flex flex-col gap-1.5 border-t py-2.5">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="text-sm font-medium">Support (internal)</span>
                  <span className="text-xs text-muted-foreground">For a support colour you lock. A customer never picks one.</span>
                </div>
                <ul className="grid grid-cols-[repeat(auto-fill,minmax(10rem,1fr))] gap-x-3 gap-y-1">
                  {support.map((filament) => (
                    <FilamentTick key={filament.id} filament={filament} {...tickProps} full onTick={(on) => tick(filament, on)} />
                  ))}
                </ul>
              </div>
            )}
          </div>
        )
      )}
    </section>
  )
}

export function ListingEditor({ draft, onChange, models, versions, schema, filaments, media, live = null, mediaState = 'ready', onPictureUploaded, coloursSettled = true, bookState = 'ready', schemaState = 'loading', shopMaterials, refusal = null }: ListingEditorProps) {
  const id = useId()
  const params = shownParams(schema)
  const offersColour = params.some((param) => param.type === 'color' && draft.params[param.name])
  const inTitle = titleColours(draft, schema, filaments)
  return (
    <div className="flex flex-col gap-5">
      <DesignLine draft={draft} onChange={onChange} models={models} versions={versions} />
      <ColourProblems draft={draft} schema={schema} filaments={filaments} onChange={onChange} />

      <section className="flex flex-col gap-3" aria-label="Product">
        <h3 className="text-sm font-semibold">Product</h3>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-title`}>Title</Label>
          <Input id={`${id}-title`} value={draft.title} maxLength={200} onChange={(event) => onChange({ ...draft, title: event.target.value })} />
          {inTitle.length > 0 && (
            <p className="flex flex-wrap items-center gap-2 text-xs text-warn" data-testid="title-colours">
              Leave {inTitle.join(' and ')} out of the title: the customer chooses the colour.
              <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={() => onChange({ ...draft, title: withoutWords(draft.title, inTitle) })}>
                Take it out
              </Button>
            </p>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-description`}>Description</Label>
          <Textarea id={`${id}-description`} value={draft.description} maxLength={5000} rows={3} onChange={(event) => onChange({ ...draft, description: event.target.value })} />
        </div>
        <ListingPictures
          slug={draft.slug}
          media={media}
          mediaState={mediaState}
          chosen={draft.media_ids}
          live={live}
          onChange={(media_ids) => onChange({ ...draft, media_ids })}
          onUploaded={(model, id) => onPictureUploaded?.(model, id)}
        />
      </section>

      <section className="flex flex-col gap-2" aria-label="Settings">
        <h3 className="text-sm font-semibold">Settings</h3>
        {!schema && draft.commit && schemaState === 'loading' && <p className="text-xs text-muted-foreground">Loading the design’s settings…</p>}
        {!schema && draft.commit && schemaState === 'failed' && (
          <p className="text-xs text-warn">The design’s settings could not be read. Choose another version, or reload the page to try again.</p>
        )}
        {schema && params.length === 0 && <p className="text-xs text-muted-foreground">This design has no settings: it is sold as it is.</p>}
        {params.length > 0 && (
          <p className="text-xs text-muted-foreground">
            In the design’s order. With “Customer can change” on, the customer chooses within the bounds you set; off, every customer gets the value you set.
          </p>
        )}
        {/* #6290 — one list in template order: a switch never moves its parameter. */}
        <ul className="flex flex-col gap-2">
          {params.map((param) => (
            <ParamRow key={param.name} param={param} draft={draft} schema={schema} filaments={filaments} onChange={onChange} />
          ))}
        </ul>
      </section>

      <MaterialsAllowed draft={draft} shop={shopMaterials} filaments={filaments} refusal={refusal} onChange={onChange} />
      <ColoursOnOffer
        draft={draft}
        schema={schema}
        filaments={filaments}
        offersColour={offersColour}
        settled={coloursSettled}
        bookState={bookState}
        materials={effectiveMaterials(draft, shopMaterials)}
        refusal={refusal}
        onChange={onChange}
      />
    </div>
  )
}
