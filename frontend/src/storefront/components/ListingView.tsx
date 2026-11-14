import { useId, type ElementType } from 'react'
import type { Listing, ParamValue } from '../api/client'
import { listingMaterials, listingUnavailable } from '../params'
import { ParamField } from './ParamField'

export type ParamValues = Record<string, ParamValue>

export interface ListingViewProps {
  listing: Listing
  /** Only the values the visitor set: a field with none shows the shop's choice. */
  values: ParamValues
  onChange: (name: string, next: ParamValue | undefined) => void
  /** The title's heading level: 1 on the public page, 2 inside the back office's preview. */
  headingLevel?: 1 | 2
  /** The filament picked for each colour parameter, by id (#6152). */
  picks?: Record<string, string>
  onPick?: (name: string, next: string | undefined) => void
  /** With `one_material`: the material chosen; null or absent is the listing's first. */
  material?: string | null
  onMaterial?: (next: string) => void
  /** What changed when the material did, said under the control. */
  materialNote?: string | null
}

/**
 * #5282 (spec 2026-11-02 §5.3, §5.4) — one listing as a visitor sees it: its title, its
 * description, its pictures, and a field for each parameter the customer may change. The
 * public page (`pages/CustomizePage.tsx`) mounts it, and so does the back office's
 * Storefront page as its live preview, so the shop sees the customer's form. It fetches
 * nothing and is styled with the theme's tokens only.
 *
 * Everything the shop typed (title, description, captions, labels) renders as React text,
 * never as HTML (spec §5.8).
 */
export function ListingView({
  listing,
  values,
  onChange,
  headingLevel = 1,
  picks,
  onPick,
  material,
  onMaterial,
  materialNote,
}: ListingViewProps) {
  const id = useId()
  const Heading: ElementType = headingLevel === 1 ? 'h1' : 'h2'
  const params = Object.entries(listing.params)
  const materials = listingMaterials(listing)
  const shownMaterial = material ?? materials[0] ?? null
  // #6152: one Material control above the colours, only when it chooses something.
  const oneMaterial = listing.one_material && materials.length > 1
  return (
    <article className="flex min-w-0 flex-col gap-5">
      <header className="flex flex-col gap-2">
        <Heading className="sf-layered m-0 text-[clamp(1.8rem,6vw,2.6rem)] leading-[1.05] font-extrabold tracking-tight [overflow-wrap:anywhere]">
          {listing.title}
        </Heading>
        {listing.description && <p className="m-0 whitespace-pre-line text-muted-foreground [overflow-wrap:anywhere]">{listing.description}</p>}
      </header>
      {listing.media.length > 0 && (
        <ul className="m-0 flex list-none gap-2 overflow-x-auto p-0" aria-label="Pictures">
          {listing.media.map((media) => (
            <li key={media.url} className="shrink-0">
              <figure className="m-0 flex w-36 flex-col gap-1">
                {media.kind === 'video' ? (
                  <video src={media.url} controls muted playsInline className="aspect-square w-36 rounded-md bg-muted object-cover" />
                ) : (
                  <img src={media.url} alt={media.caption} className="aspect-square w-36 rounded-md bg-muted object-cover" />
                )}
                {media.caption && <figcaption className="text-xs text-muted-foreground">{media.caption}</figcaption>}
              </figure>
            </li>
          ))}
        </ul>
      )}
      {listingUnavailable(listing) && (
        <p className="m-0 font-medium text-destructive" role="status">
          This design cannot be ordered right now.
        </p>
      )}
      {params.length > 0 ? (
        <form className="grid gap-[18px]" aria-label="Customize" onSubmit={(event) => event.preventDefault()}>
          {oneMaterial && (
            <div className="min-w-0">
              <p className="mb-1.5 block p-0 font-semibold" id={`${id}-material`}>
                Material
              </p>
              <div className="flex flex-wrap gap-2" role="radiogroup" aria-labelledby={`${id}-material`}>
                {materials.map((each) => (
                  <button
                    key={each}
                    type="button"
                    role="radio"
                    aria-checked={each === shownMaterial}
                    onClick={() => onMaterial?.(each)}
                    className="min-h-11 rounded-full border-[1.5px] border-input px-4 aria-checked:border-primary aria-checked:ring-1 aria-checked:ring-primary"
                  >
                    {each}
                  </button>
                ))}
              </div>
              {materialNote && (
                <p className="mt-1.5 text-sm text-muted-foreground" role="status">
                  {materialNote}
                </p>
              )}
            </div>
          )}
          {params.map(([name, param]) => (
            <ParamField
              key={name}
              param={param}
              value={values[name]}
              filaments={listing.colours}
              material={listing.one_material ? shownMaterial : null}
              pick={picks?.[name]}
              onPick={onPick && ((next) => onPick(name, next))}
              onChange={(next) => onChange(name, next)}
            />
          ))}
        </form>
      ) : (
        <p className="m-0 text-sm text-muted-foreground">This design is made as shown.</p>
      )}
    </article>
  )
}
