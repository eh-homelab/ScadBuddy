import { useId } from 'react'
import type { CustomizerSchema, Filament, ListingBound, MediaView, ModelSummary, ModelVersion, Param, ParamValue } from '../../api/types'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { emptyDraft, lockedValue, MAX_TEXT_LENGTH, offerable, schemaBound, shownParams, type ListingDraft } from './listingDraft'

/**
 * #5282 (spec 2026-11-02 §5.3) — one storefront listing in the Storefront page's editor:
 * the template and the commit it sells, then each parameter "Locked at …" or "Customer may
 * change" (with bounds inside the schema's own), the filaments a customer may pick, the
 * media to show, the title and the description. Controlled: the page holds the draft,
 * loads the schema, the price book and the media, and saves.
 */
export interface ListingEditorProps {
  draft: ListingDraft
  onChange: (draft: ListingDraft) => void
  models: readonly ModelSummary[]
  versions: readonly ModelVersion[] | undefined
  schema: CustomizerSchema | undefined
  filaments: readonly Filament[]
  media: readonly MediaView[]
}

const LOCKED = 'locked'
const OFFERED = 'offered'

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

function BoundFields({
  param,
  bound,
  hasFilaments,
  onChange,
}: {
  param: Param
  bound: ListingBound
  hasFilaments: boolean
  onChange: (bound: ListingBound) => void
}) {
  const id = useId()
  const label = param.caption || param.name
  if (param.type === 'color') {
    return (
      <p className="text-xs text-muted-foreground">
        {hasFilaments ? 'The customer picks one of the listing’s filaments.' : 'Choose the filaments below first.'}
      </p>
    )
  }
  if (bound.kind === 'number') {
    return (
      <div className="grid grid-cols-3 gap-2">
        {(['min', 'max', 'step'] as const).map((key) => (
          <div key={key} className="flex flex-col gap-1">
            <Label htmlFor={`${id}-${key}`} className="text-xs font-normal">
              {key === 'min' ? 'Least' : key === 'max' ? 'Most' : 'Step'}
            </Label>
            <Input
              id={`${id}-${key}`}
              type="number"
              aria-label={`${key === 'min' ? 'Least' : key === 'max' ? 'Most' : 'Step'} ${label}`}
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
      <div className="flex flex-col gap-1">
        <Label htmlFor={id} className="text-xs font-normal">
          Longest text
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
 * (each kept as its option's own value, so a numeric select stays a number), else text.
 */
function LockedField({ id, param, label, value, onChange }: { id: string; param: Param; label: string; value: ParamValue | undefined; onChange: (value: ParamValue) => void }) {
  if (param.type === 'boolean') {
    return <Switch id={id} aria-label={`Locked value of ${label}`} checked={value === true} onCheckedChange={(on) => onChange(on)} />
  }
  const options = param.options ?? []
  if (param.type === 'select' && options.length > 0) {
    const at = options.findIndex((option) => option.value === value)
    return (
      <Select value={at < 0 ? undefined : String(at)} onValueChange={(index) => {
        const option = options[Number(index)]
        if (option) onChange(option.value)
      }}>
        <SelectTrigger id={id} aria-label={`Locked value of ${label}`} className="w-full">
          <SelectValue placeholder="Choose" />
        </SelectTrigger>
        <SelectContent>
          {options.map((option, index) => (
            <SelectItem key={index} value={String(index)}>
              {option.name || String(option.value)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  }
  return <Input id={id} aria-label={`Locked value of ${label}`} value={value === undefined ? '' : String(value)} onChange={(event) => onChange(parsed(param, event.target.value))} />
}

function ParamRow({
  param,
  draft,
  onChange,
}: {
  param: Param
  draft: ListingDraft
  onChange: (draft: ListingDraft) => void
}) {
  const id = useId()
  const label = param.caption || param.name
  const bound = draft.params[param.name]
  const value = lockedValue(draft, param)
  const canOffer = offerable(param) && (param.type !== 'color' || draft.colours.length > 0)

  function lock() {
    const { [param.name]: _dropped, ...params } = draft.params
    onChange({ ...draft, params, fixed: value === undefined ? draft.fixed : { ...draft.fixed, [param.name]: value } })
  }

  function offer() {
    const { [param.name]: _dropped, ...fixed } = draft.fixed
    onChange({ ...draft, fixed, params: { ...draft.params, [param.name]: schemaBound(param) } })
  }

  return (
    <fieldset className="flex flex-col gap-2 rounded-md border p-3" aria-labelledby={`${id}-name`}>
      <legend id={`${id}-name`} className="px-1 text-sm font-medium">
        {label}
        {param.caption && <span className="ml-1 font-mono text-xs font-normal text-muted-foreground">{param.name}</span>}
      </legend>
      <RadioGroup aria-labelledby={`${id}-name`} value={bound ? OFFERED : LOCKED} onValueChange={(next) => (next === OFFERED ? offer() : lock())} className="flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-sm">
          <RadioGroupItem value={LOCKED} aria-label={`Locked at ${shown(value)}`} />
          Locked at {shown(value)}
        </label>
        <label className="flex items-center gap-2 text-sm has-[:disabled]:opacity-60">
          <RadioGroupItem value={OFFERED} disabled={!canOffer} aria-label="Customer may change" />
          Customer may change
        </label>
      </RadioGroup>
      {bound ? (
        <BoundFields param={param} bound={bound} hasFilaments={draft.colours.length > 0} onChange={(next) => onChange({ ...draft, params: { ...draft.params, [param.name]: next } })} />
      ) : (
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-value`} className="text-xs font-normal">
            Value
          </Label>
          <LockedField id={`${id}-value`} param={param} label={label} value={value} onChange={(next) => onChange({ ...draft, fixed: { ...draft.fixed, [param.name]: next } })} />
        </div>
      )}
    </fieldset>
  )
}

export function ListingEditor({ draft, onChange, models, versions, schema, filaments, media }: ListingEditorProps) {
  const id = useId()
  const params = shownParams(schema)
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-model`}>Template</Label>
          <Select value={draft.slug || undefined} onValueChange={(slug) => onChange({ ...emptyDraft(slug), title: draft.title, description: draft.description })}>
            <SelectTrigger id={`${id}-model`} className="w-full">
              <SelectValue placeholder="Choose a template" />
            </SelectTrigger>
            <SelectContent>
              {models.map((model) => (
                <SelectItem key={model.slug} value={model.slug}>
                  {model.name}
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
              {(versions ?? []).map((version) => (
                <SelectItem key={version.commit} value={version.commit}>
                  {version.short} · {version.message}
                  {version.current ? ' (current)' : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-title`}>Title</Label>
        <Input id={`${id}-title`} value={draft.title} maxLength={200} onChange={(event) => onChange({ ...draft, title: event.target.value })} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-description`}>Description</Label>
        <Textarea id={`${id}-description`} value={draft.description} maxLength={5000} rows={3} onChange={(event) => onChange({ ...draft, description: event.target.value })} />
      </div>

      <section className="flex flex-col gap-2" aria-label="Parameters">
        <h3 className="text-sm font-medium">Parameters</h3>
        {!schema && draft.commit && <p className="text-xs text-muted-foreground">Loading the template’s parameters…</p>}
        {schema && params.length === 0 && <p className="text-xs text-muted-foreground">This template has no parameters: it is sold as it is.</p>}
        {params.map((param) => (
          <ParamRow key={param.name} param={param} draft={draft} onChange={onChange} />
        ))}
      </section>

      <section className="flex flex-col gap-2" aria-label="Filaments">
        <h3 className="text-sm font-medium">Filaments</h3>
        {filaments.length === 0 ? (
          <p className="text-xs text-muted-foreground">The price book has no filaments yet.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {filaments.map((filament) => {
              const name = `${filament.material} ${filament.colour_name ?? filament.colour}`
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
                    <span aria-hidden="true" className="inline-block size-3 rounded-sm border" style={{ background: filament.colour }} />
                    {name}
                  </label>
                </li>
              )
            })}
          </ul>
        )}
        {/* #6152: every colour of an order is of one material; the customer picks it once. */}
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={draft.one_material} onCheckedChange={(on) => onChange({ ...draft, one_material: on === true })} />
          One material for every colour
        </label>
      </section>

      <section className="flex flex-col gap-2" aria-label="Media">
        <h3 className="text-sm font-medium">Media</h3>
        {media.length === 0 ? (
          <p className="text-xs text-muted-foreground">This template has no pictures or videos.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {media.map((item) => {
              const name = item.caption || item.file
              return (
                <li key={item.id}>
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={draft.media_ids.includes(item.id)}
                      aria-label={`Show ${name}`}
                      onCheckedChange={(on) =>
                        onChange({ ...draft, media_ids: on === true ? [...draft.media_ids, item.id] : draft.media_ids.filter((m) => m !== item.id) })
                      }
                    />
                    {name}
                  </label>
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </div>
  )
}
