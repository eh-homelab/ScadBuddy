import { useId } from 'react'
import type { ListingBound, ParamValue } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/**
 * #5282 (spec 2026-11-02 §5.3) — one listing as a visitor sees it: its title, its
 * description, its media, and a field for each parameter the customer may change, inside
 * the listing's bounds. It reads only its props and fetches nothing, so the back office's
 * Storefront page mounts it with the unsaved listing as a live preview, and the public
 * page mounts it with what the storefront serves.
 *
 * Everything the shop typed (title, description, captions, labels) renders as React text,
 * never as HTML (spec §5.8).
 */

export type ParamValues = Record<string, ParamValue>

/** A parameter the customer may change (its bound is `orders/storefront/wire.py` `ParamBound`), with its schema's label, help and group. */
export type PublicParam = ListingBound & {
  label: string
  help?: string | null
  group?: string
  /** A `// color` parameter: its choices are the colours of the listing's filaments. */
  colour?: boolean
}

export interface PublicFilament {
  id: string
  colour: string
  colour_name: string | null
  material: string
}

export interface PublicMedia {
  url: string
  kind: 'image' | 'video'
  caption: string
}

export interface PublicListing {
  id: string
  title: string
  description: string
  params: Record<string, PublicParam>
  colours: PublicFilament[]
  media: PublicMedia[]
  updated_at: string
}

/** The fields of a render the view shows. */
export interface PreviewJob {
  id: string
  status: string
  error?: string | null
}

/** A rough price: shown as an estimate, never as a quote (spec §5.4). */
export interface EstimateView {
  price_minor: number
  currency: string
  minor_exponent: number
}

export interface ListingViewProps {
  listing: PublicListing
  values: ParamValues
  onChange?: (values: ParamValues) => void
  job?: PreviewJob
  estimate?: EstimateView | null
}

function filamentName(filament: PublicFilament): string {
  return [filament.material, filament.colour_name ?? filament.colour].join(' ')
}

function ParamField({
  name,
  param,
  value,
  colours,
  onChange,
}: {
  name: string
  param: PublicParam
  value: ParamValue | undefined
  colours: PublicFilament[]
  onChange?: (next: ParamValue) => void
}) {
  const id = useId()
  const disabled = onChange === undefined
  let field
  if (param.kind === 'number') {
    field = (
      <Input
        id={id}
        type="number"
        min={param.min}
        max={param.max}
        step={param.step ?? 'any'}
        value={typeof value === 'number' ? value : ''}
        disabled={disabled}
        onChange={(event) => {
          const next = event.target.valueAsNumber
          if (!Number.isNaN(next)) onChange?.(next)
        }}
      />
    )
  } else if (param.kind === 'text') {
    field = (
      <Input
        id={id}
        value={typeof value === 'string' ? value : ''}
        maxLength={param.max_length}
        disabled={disabled}
        onChange={(event) => onChange?.(event.target.value)}
      />
    )
  } else {
    const named = (choice: ParamValue): string => {
      if (!param.colour) return String(choice)
      const filament = colours.find((f) => f.colour.toLowerCase() === String(choice).toLowerCase())
      return filament ? filamentName(filament) : String(choice)
    }
    field = (
      <Select
        value={value === undefined ? undefined : String(value)}
        disabled={disabled}
        onValueChange={(next) => onChange?.(param.choices.find((choice) => String(choice) === next) ?? next)}
      >
        <SelectTrigger id={id} className="w-full">
          <SelectValue placeholder="Choose" />
        </SelectTrigger>
        <SelectContent>
          {param.choices.map((choice) => (
            <SelectItem key={String(choice)} value={String(choice)}>
              {param.colour && (
                <span aria-hidden="true" className="inline-block size-3 rounded-sm border" style={{ background: String(choice) }} />
              )}
              {named(choice)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  }
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{param.label || name}</Label>
      {field}
      {param.help && <p className="text-xs text-muted-foreground">{param.help}</p>}
    </div>
  )
}

export function ListingView({ listing, values, onChange, job, estimate }: ListingViewProps) {
  const params = Object.entries(listing.params)
  return (
    <article className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold tracking-tight break-words">{listing.title}</h2>
        {listing.description && <p className="text-sm whitespace-pre-line break-words">{listing.description}</p>}
      </header>
      {listing.media.length > 0 && (
        <ul className="flex gap-2 overflow-x-auto" aria-label="Pictures">
          {listing.media.map((media) => (
            <li key={media.url} className="shrink-0">
              <figure className="flex w-40 flex-col gap-1">
                {media.kind === 'video' ? (
                  <video src={media.url} controls muted playsInline className="aspect-square w-40 rounded-md bg-muted object-cover" />
                ) : (
                  <img src={media.url} alt={media.caption} className="aspect-square w-40 rounded-md bg-muted object-cover" />
                )}
                {media.caption && <figcaption className="text-xs text-muted-foreground">{media.caption}</figcaption>}
              </figure>
            </li>
          ))}
        </ul>
      )}
      {params.length > 0 ? (
        <form className="flex flex-col gap-3" aria-label="Customize" onSubmit={(event) => event.preventDefault()}>
          {params.map(([name, param]) => (
            <ParamField
              key={name}
              name={name}
              param={param}
              value={values[name]}
              colours={listing.colours}
              onChange={onChange ? (next) => onChange({ ...values, [name]: next }) : undefined}
            />
          ))}
        </form>
      ) : (
        <p className="text-sm text-muted-foreground">Made as shown.</p>
      )}
      {listing.colours.length > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className="text-sm font-medium">Colours</h3>
          <ul className="flex flex-wrap gap-2 text-xs">
            {listing.colours.map((filament) => (
              <li key={filament.id} className="flex items-center gap-1">
                <span aria-hidden="true" className="inline-block size-3 rounded-sm border" style={{ background: filament.colour }} />
                {filamentName(filament)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {job && (
        <p className="text-sm" role="status">
          {job.status === 'failed' ? `The preview failed${job.error ? `: ${job.error}` : '.'}` : job.status === 'done' ? 'Preview ready.' : 'Making the preview…'}
        </p>
      )}
      {estimate && (
        <p className="text-sm font-medium">
          about {formatMoney(estimate.price_minor, estimate.currency, estimate.minor_exponent)}, confirmed after we check it
        </p>
      )}
    </article>
  )
}
