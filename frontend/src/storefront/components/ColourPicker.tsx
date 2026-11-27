import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover'
import { ToggleGroup, ToggleGroupItem } from '../../components/ui/toggle-group'
import type { ColourSwatch } from '../colours'

/** Past this many colours, the picker offers a search. */
const SEARCH_FROM = 24
/** The grid's columns: the arrow keys move up and down by this many. */
const COLUMNS = 4

export interface ColourPickerProps {
  /** The parameter's label, as the customer reads it. */
  label: string
  /** The colours the customer may pick for this part: one per colour, whatever its material (`colourSwatches`). */
  swatches: readonly ColourSwatch[]
  /** Whether the customer picked this part; false while the shop's choice stands. */
  picked: boolean
  /** The colour the row shows: the pick, else the shop's choice (which can follow another part's pick, #6258). */
  shown: string | undefined
  /** A colour picked, or undefined to go back to the shop's choice. */
  onChange: (next: ColourSwatch | undefined) => void
  /**
   * The colours the design uses now (every part's colour on screen, casefolded), which the
   * grid shows first (#8127). Empty: one plain grid.
   */
  used?: readonly string[]
  /** What the row says when `shown` is no swatch: "Choose a colour", or "Mixed colours" for a group. */
  empty?: string
  /** The words of the button that goes back to the shop's choice. */
  reset?: string
  /** A line under the row's colour: "No colour is available for this right now." */
  problem?: string
  /** The row's look: a part (`part`), or the row that sets every part of a group (`all`). */
  variant?: 'part' | 'all'
}

/**
 * #6152, #6258, #8127: one colour part of a design. It is a compact row (the colour, the
 * part's name and the colour's name), which opens a grid of swatches: first the colours the
 * design already uses, then the rest. A colour in two materials is one swatch, named by its
 * colour. The customer never sees a material: not on the row, not in the grid (owner
 * ruling, #6258). No support material is ever offered. Styled with the theme's tokens only.
 */
export function ColourPicker({
  label,
  swatches,
  picked,
  shown,
  onChange,
  used = [],
  empty = 'Choose a colour',
  reset = 'Use the shop’s choice',
  problem,
  variant = 'part',
}: ColourPickerProps) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const grid = useRef<HTMLDivElement>(null)
  const current = swatches.find((swatch) => swatch.key === shown?.toLowerCase())

  const query = search.trim().toLowerCase()
  const visible = swatches.filter((swatch) => query === '' || swatch.name.toLowerCase().includes(query))
  const inDesign = new Set(used)
  const first = used.flatMap((key) => visible.filter((swatch) => swatch.key === key))
  const rest = visible.filter((swatch) => !inDesign.has(swatch.key))
  // Two sections only when each holds a colour: else one plain grid.
  const sections: { title: string | null; swatches: ColourSwatch[] }[] =
    first.length > 0 && rest.length > 0
      ? [
          { title: 'In this design', swatches: first },
          { title: 'More colours', swatches: rest },
        ]
      : [{ title: null, swatches: [...first, ...rest] }]

  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(grid.current?.querySelectorAll<HTMLButtonElement>('[data-slot="toggle-group-item"]') ?? [])
    const at = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = at < 0 ? items[0] : neighbour(items, at, event.key)
    if (next === null) return
    if (next) {
      // Before the item's own roving focus, which moves one step only.
      event.preventDefault()
      event.stopPropagation()
      next.focus()
    }
  }

  const all = variant === 'all'
  return (
    <div className="min-w-0">
      <Popover
        open={open}
        onOpenChange={(next) => {
          setOpen(next)
          if (!next) setSearch('')
        }}
      >
        <PopoverTrigger
          type="button"
          aria-labelledby={`${id}-label ${id}-value`}
          aria-describedby={picked ? `${id}-mark` : undefined}
          className={
            'flex min-h-12 w-full items-center gap-3 rounded-md px-2.5 py-1.5 text-left hover:bg-muted data-[state=open]:bg-muted ' +
            (all ? 'border border-dashed border-input' : '')
          }
        >
          <Dot style={current?.style} size={all ? 'size-6' : 'size-7'} />
          <span className="min-w-0 flex-1">
            <span id={`${id}-label`} className={'block leading-snug [overflow-wrap:anywhere] ' + (all ? 'text-sm' : 'font-medium')}>
              {label}
            </span>
            <span id={`${id}-value`} className="block text-sm leading-snug text-muted-foreground [overflow-wrap:anywhere]">
              {current ? current.name : empty}
            </span>
            {problem && <span className="block text-sm leading-snug text-destructive">{problem}</span>}
          </span>
          {picked && (
            <span id={`${id}-mark`} className="flex-none rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground">
              {all ? 'Your picks' : 'Your pick'}
            </span>
          )}
          <Chevron />
        </PopoverTrigger>
        <PopoverContent
          className="w-[min(22rem,calc(100vw-32px))]"
          align="start"
          aria-label={`Colours for ${label}`}
          // The colour on the row takes the focus, not the search: a phone opens no keyboard
          // over the grid until the visitor asks for one.
          onOpenAutoFocus={(event) => {
            const on = grid.current?.querySelector<HTMLButtonElement>('[data-state="on"]') ?? grid.current?.querySelector<HTMLButtonElement>('button')
            if (!on) return
            event.preventDefault()
            on.focus()
          }}
        >
          <p className="m-0 font-semibold [overflow-wrap:anywhere]">{label}</p>
          {swatches.length > SEARCH_FROM && (
            <input
              type="search"
              aria-label="Find a colour"
              placeholder="Find a colour"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className="min-h-10 w-full rounded-md border border-input bg-background px-3"
            />
          )}
          <ToggleGroup
            ref={grid}
            type="single"
            aria-label={label}
            loop={false}
            onKeyDownCapture={move}
            // The shop's choice shows as picked until the customer picks (#6144); tapping
            // it empties the group, which keeps the shop's choice.
            value={current?.key ?? ''}
            onValueChange={(key) => {
              onChange(key === '' ? undefined : swatches.find((swatch) => swatch.key === key))
              setOpen(false)
            }}
            className="flex max-h-[50vh] w-full flex-col items-stretch gap-2 overflow-y-auto p-0.5"
          >
            {sections.map((section, index) => (
              <Section key={section.title ?? 'all'} title={section.title}>
                {section.swatches.map((swatch, at) => (
                  <ToggleGroupItem
                    key={swatch.key}
                    value={swatch.key}
                    aria-label={swatch.name}
                    data-section={index}
                    data-index={at}
                    className="flex h-auto min-w-0 flex-col items-center justify-start gap-1 self-stretch rounded-md p-1 text-center text-xs font-normal whitespace-normal data-[state=on]:bg-transparent data-[state=on]:ring-2 data-[state=on]:ring-primary"
                  >
                    <Dot style={swatch.style} size="size-10" />
                    <span className="w-full leading-tight [overflow-wrap:anywhere]">{swatch.name}</span>
                  </ToggleGroupItem>
                ))}
              </Section>
            ))}
            {visible.length === 0 && <p className="m-0 text-sm text-muted-foreground">No colour matches.</p>}
          </ToggleGroup>
          {picked && (
            <button
              type="button"
              className="self-start text-sm underline"
              onClick={() => {
                onChange(undefined)
                setOpen(false)
              }}
            >
              {reset}
            </button>
          )}
        </PopoverContent>
      </Popover>
    </div>
  )
}

/** One section of the grid: its title, if it has one, over its swatches in rows of `COLUMNS`. */
function Section({ title, children }: { title: string | null; children: ReactNode }) {
  const grid = <div className="grid w-full grid-cols-4 gap-2">{children}</div>
  if (title === null) return grid
  return (
    <div className="flex flex-col gap-1.5" role="presentation">
      <p className="m-0 text-xs font-medium text-muted-foreground">{title}</p>
      {grid}
    </div>
  )
}

/**
 * The swatch an arrow key moves to from `items[at]`: left and right go one step, up and
 * down one row in the same column. From a section's first or last row, up or down goes to
 * the nearest swatch in the same column of the next section. Null: not an arrow key.
 * Undefined: no swatch that way.
 */
function neighbour(items: readonly HTMLButtonElement[], at: number, key: string): HTMLButtonElement | null | undefined {
  if (key === 'ArrowRight') return items[at + 1]
  if (key === 'ArrowLeft') return items[at - 1]
  if (key !== 'ArrowDown' && key !== 'ArrowUp') return null
  const place = (item: HTMLButtonElement) => [Number(item.dataset.section ?? 0), Number(item.dataset.index ?? 0)] as const
  const [section, index] = place(items[at]!)
  const of = (s: number) => items.filter((item) => place(item)[0] === s)
  const column = index % COLUMNS
  if (key === 'ArrowDown') {
    const here = of(section)
    if (index + COLUMNS < here.length) return here[index + COLUMNS]
    const below = of(section + 1)
    return below[Math.min(column, below.length - 1)]
  }
  if (index - COLUMNS >= 0) return of(section)[index - COLUMNS]
  const above = of(section - 1)
  if (above.length === 0) return undefined
  const lastRow = Math.floor((above.length - 1) / COLUMNS) * COLUMNS
  return above[Math.min(lastRow + column, above.length - 1)]
}

/** A colour's round swatch; none drawn (an empty ring) while no colour is set. */
export function Dot({ style, size }: { style: ColourSwatch['style'] | undefined; size: string }) {
  return <span className={`${size} flex-none rounded-full border border-foreground/25`} style={style} aria-hidden="true" />
}

/** The row's opener mark: a small chevron in the text colour. */
function Chevron() {
  return (
    <svg className="size-4 flex-none text-muted-foreground" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M6 3.5 10.5 8 6 12.5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
