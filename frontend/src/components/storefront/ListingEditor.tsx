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
import { colourProblems, emptyDraft, fixColour, isSupport, isSupportOnly, lockedValue, MAX_TEXT_LENGTH, nearestFilaments, offerable, schemaBound, shownParams, titleColours, withoutWords, type ListingDraft } from './listingDraft'
import { ListingPictures } from './ListingPictures'

/**
 * #5282 (spec 2026-11-02 §5.3), #6131 (design 2026-11-13 §5) — one storefront listing in
 * the Storefront page's editor, in the order a shop thinks: the design it sells (one line,
 * "Change" opens the template and version), the product (title, description, pictures),
 * what the customer chooses (each offered parameter's bounds and the colours on offer),
 * and what the shop sets (each locked parameter's value). One switch per parameter moves
 * it between the two. A colour that publish would refuse is named first, with a filament
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
  /** #6213 — the pictures the public site shows now, or null while the listing is not published. */
  live?: readonly string[] | null
  /** #6213 — the template as a picture uploaded in the editor left it. */
  onMediaUploaded?: (model: ModelSummary) => void
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
    return 'Tick a colour on offer first.'
  }
  return null
}

function ParamRow({ param, draft, schema, filaments, onChange }: { param: Param; draft: ListingDraft; schema: CustomizerSchema | undefined; filaments: readonly Filament[]; onChange: (draft: ListingDraft) => void }) {
  const id = useId()
  const label = param.caption || param.name
  const bound = draft.params[param.name]
  const value = lockedValue(draft, param)
  const why = bound ? null : whyNotOffered(param, draft, schema, filaments)

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
        <Select value={draft.slug || undefined} onValueChange={(slug) => onChange({ ...emptyDraft(slug), title: draft.title, description: draft.description })}>
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

export function ListingEditor({ draft, onChange, models, versions, schema, filaments, media, live = null, onMediaUploaded }: ListingEditorProps) {
  const id = useId()
  const params = shownParams(schema)
  const offered = params.filter((param) => draft.params[param.name])
  const locked = params.filter((param) => !draft.params[param.name])
  const offersColour = offered.some((param) => param.type === 'color')
  const inTitle = titleColours(draft, schema, filaments)
  /** The locked parameters' rows: folded to one line until asked for, since most products need none changed. */
  const [showLocked, setShowLocked] = useState(false)
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
          chosen={draft.media_ids}
          live={live}
          onChange={(media_ids) => onChange({ ...draft, media_ids })}
          onUploaded={(model) => onMediaUploaded?.(model)}
        />
      </section>

      <section className="flex flex-col gap-2" aria-label="What the customer chooses">
        <h3 className="text-sm font-semibold">What the customer chooses</h3>
        {!schema && draft.commit && <p className="text-xs text-muted-foreground">Loading the design’s settings…</p>}
        {schema && params.length === 0 && <p className="text-xs text-muted-foreground">This design has no settings: it is sold as it is.</p>}
        {schema && params.length > 0 && offered.length === 0 && (
          <p className="text-xs text-muted-foreground">Nothing: every customer gets the product as you set it below.</p>
        )}
        <ul className="flex flex-col gap-2">
          {offered.map((param) => (
            <ParamRow key={param.name} param={param} draft={draft} schema={schema} filaments={filaments} onChange={onChange} />
          ))}
        </ul>
        <div className="flex flex-col gap-1" role="group" aria-label="Colours on offer">
          <span className="text-sm font-medium">Colours on offer</span>
          <p className="text-xs text-muted-foreground">
            {offersColour ? 'The filaments a customer can pick from.' : 'The filaments this product is printed in.'}
          </p>
          {filaments.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              The price book has no filaments yet.{' '}
              <Link to={PRICE_BOOK_PATH} className="underline">
                Add one
              </Link>
              .
            </p>
          ) : (
            <ul className="flex flex-wrap gap-x-4 gap-y-1">
              {filaments.map((filament) => {
                const name = filamentName(filament)
                return (
                  <li key={filament.id}>
                    <label className="flex items-center gap-2 text-sm">
                      <Checkbox
                        checked={draft.colours.includes(filament.id)}
                        aria-label={name}
                        onCheckedChange={(on) =>
                          onChange({ ...draft, colours: on === true ? [...draft.colours, filament.id] : draft.colours.filter((c) => c !== filament.id) })
                        }
                      />
                      <Swatch colour={filament.colour} />
                      {name}
                      {offersColour && isSupportOnly(filament, draft, schema, filaments) && (
                        <span className="text-xs text-muted-foreground">(for supports only, not offered)</span>
                      )}
                    </label>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </section>

      {locked.length > 0 && (
        <section className="flex flex-col gap-2" aria-label="Set by you">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">Set by you</h3>
            <Button variant="link" size="sm" className="h-auto p-0" aria-expanded={showLocked} aria-controls={`${id}-locked`} onClick={() => setShowLocked(!showLocked)}>
              {showLocked ? 'Hide' : `Show all ${locked.length}`}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Every customer gets these as you set them
            {showLocked ? '.' : `: ${locked.map((param) => `${param.caption || param.name} ${shown(lockedValue(draft, param))}`).join(', ')}.`}
          </p>
          {showLocked && (
            <ul id={`${id}-locked`} className="flex flex-col gap-2">
              {locked.map((param) => (
                <ParamRow key={param.name} param={param} draft={draft} schema={schema} filaments={filaments} onChange={onChange} />
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}
