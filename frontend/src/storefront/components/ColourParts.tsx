import { useId, useMemo, useState } from 'react'
import type { Listing, ParamValue } from '../api/client'
import { colourSections, GROUP_ALL_FROM, LIST_ALL_FROM, usedColours, type ColourPart } from '../colourParts'
import { colourSwatches, type ColourSwatch } from '../colours'
import { colourGone, startColour } from '../params'
import { ColourPicker } from './ColourPicker'

export interface ColourPartsProps {
  listing: Listing
  parts: readonly ColourPart[]
  /** Only the values the visitor set. */
  values: Readonly<Record<string, ParamValue>>
  /** The colour each part shows while the visitor sets none (`ListingView`: settled, else predicted). */
  shown: Readonly<Record<string, ParamValue | undefined>>
  onChange: (name: string, next: ParamValue | undefined) => void
}

/** A part with what it shows: its swatches and its colour on screen. */
interface Row extends ColourPart {
  swatches: ColourSwatch[]
  colour: string | undefined
}

const BOX = 'rounded-[10px] border-[1.5px] border-input bg-background'

/**
 * #8127: every colour part of a listing, in one block. It says once that each part is the
 * shop's choice until the visitor picks one. A long list in two or more groups shows its
 * groups (the schema's `group`), each one opening and closing, the first open; a closed
 * group shows its parts' colours as one strip. A group (or a long plain list) has a row
 * that sets one colour on all its parts; each part can then still take its own. A short
 * list is one plain list of rows.
 */
export function ColourParts({ listing, parts, values, shown, onChange }: ColourPartsProps) {
  const id = useId()
  const rows = useMemo<Row[]>(
    () =>
      parts.map((part) => {
        const value = values[part.name]
        const fallback = shown[part.name]
        const colour = typeof value === 'string' ? value : typeof fallback === 'string' ? fallback : startColour(part.param)
        return { ...part, swatches: colourSwatches(part.param, listing.colours, listing.materials), colour }
      }),
    [parts, values, shown, listing.colours, listing.materials],
  )
  const used = useMemo(() => usedColours(rows, Object.fromEntries(rows.map((row) => [row.name, row.colour]))), [rows])
  const sections = colourSections(parts)
  const byName = new Map(rows.map((row) => [row.name, row]))
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set(sections ? [sections[0]!.group] : []))

  const list = (each: readonly Row[], all: number, allLabel: string) => (
    <ul className="m-0 flex list-none flex-col gap-0.5 p-1">
      {each.length >= all && (
        <li>
          <AllRow label={allLabel} rows={each} values={values} used={used} onChange={onChange} />
        </li>
      )}
      {each.map((row) => (
        <li key={row.name}>
          <ColourPicker
            label={row.param.label}
            swatches={row.swatches}
            picked={values[row.name] !== undefined}
            shown={row.colour}
            used={used}
            problem={colourGone(row.param) ? 'No colour is available for this right now.' : undefined}
            onChange={(next) => onChange(row.name, next?.colour)}
          />
        </li>
      ))}
    </ul>
  )

  return (
    <fieldset className="m-0 flex min-w-0 flex-col gap-2 border-0 p-0" aria-describedby={`${id}-hint`}>
      <legend className="mb-0.5 p-0 font-semibold">Colours</legend>
      <p id={`${id}-hint`} className="m-0 text-sm text-muted-foreground">
        {rows.length === 1 ? "The shop's choice, unless you pick one." : "Each part is the shop's choice, unless you pick one."}
      </p>
      {sections === null ? (
        <div className={BOX}>{list(rows, LIST_ALL_FROM, `All ${rows.length} parts`)}</div>
      ) : (
        <div className="flex flex-col gap-2">
          {sections.map((section, index) => {
            const each = section.parts.map((part) => byName.get(part.name)!)
            const isOpen = open.has(section.group)
            const bodyId = `${id}-group-${index}`
            const title = section.group || 'Other colours'
            return (
              <section key={section.group} className={BOX} aria-labelledby={`${bodyId}-title`}>
                <h3 className="m-0 text-base font-normal">
                  <button
                    type="button"
                    aria-expanded={isOpen}
                    aria-controls={bodyId}
                    onClick={() =>
                      setOpen((before) => {
                        const next = new Set(before)
                        if (next.has(section.group)) next.delete(section.group)
                        else next.add(section.group)
                        return next
                      })
                    }
                    className="flex min-h-12 w-full items-center gap-3 rounded-[8px] px-3.5 py-2 text-left hover:bg-muted"
                  >
                    <span className="flex min-w-0 flex-1 flex-col gap-1.5">
                      <span className="flex flex-wrap items-baseline gap-x-2">
                        <span id={`${bodyId}-title`} className="font-semibold [overflow-wrap:anywhere]">
                          {title}
                        </span>
                        <span className="text-sm text-muted-foreground">{each.length === 1 ? '1 part' : `${each.length} parts`}</span>
                      </span>
                      {!isOpen && <Strip rows={each} />}
                    </span>
                    <svg
                      className={'size-4 flex-none text-muted-foreground transition-transform ' + (isOpen ? 'rotate-90' : '')}
                      viewBox="0 0 16 16"
                      fill="none"
                      aria-hidden="true"
                    >
                      <path d="M6 3.5 10.5 8 6 12.5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                </h3>
                <div id={bodyId} hidden={!isOpen} className="border-t border-input">
                  {isOpen && list(each, GROUP_ALL_FROM, `All ${each.length} parts`)}
                </div>
              </section>
            )
          })}
        </div>
      )}
    </fieldset>
  )
}

/** A closed group's colours: one band per part, in the parts' order, as a strip of filament. */
function Strip({ rows }: { rows: readonly Row[] }) {
  return (
    <span className="flex h-3 w-full max-w-56 overflow-hidden rounded-full border border-foreground/20" aria-hidden="true">
      {rows.map((row) => {
        const swatch = row.swatches.find((each) => each.key === row.colour?.toLowerCase())
        return <span key={row.name} className="h-full flex-1 border-r border-foreground/10 last:border-r-0" style={swatch?.style} />
      })}
    </span>
  )
}

/**
 * The row that sets one colour on every part of `rows`. It offers the colours each part
 * offers, shows their one colour when they share it (else "Mixed colours"), and sends each
 * part's pick. Going back to the shop's choice takes every part back.
 */
function AllRow({
  label,
  rows,
  values,
  used,
  onChange,
}: {
  label: string
  rows: readonly Row[]
  values: Readonly<Record<string, ParamValue>>
  used: readonly string[]
  onChange: (name: string, next: ParamValue | undefined) => void
}) {
  const [first, ...others] = rows
  const swatches = (first?.swatches ?? []).filter((swatch) => others.every((row) => row.swatches.some((each) => each.key === swatch.key)))
  const colours = new Set(rows.map((row) => row.colour?.toLowerCase()))
  const one = colours.size === 1 ? [...colours][0] : undefined
  if (swatches.length === 0) return null
  return (
    <ColourPicker
      variant="all"
      label={label}
      swatches={swatches}
      picked={rows.some((row) => values[row.name] !== undefined)}
      shown={one}
      empty="Mixed colours"
      reset="Use the shop’s choice for all"
      used={used}
      onChange={(next) => {
        for (const row of rows) onChange(row.name, next?.colour)
      }}
    />
  )
}
