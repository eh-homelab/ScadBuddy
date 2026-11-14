import { useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover'
import type { Filament } from '../api/client'
import { customerFilaments, filamentLabels } from '../params'

/** Past this many colours, the picker offers a search. */
const SEARCH_FROM = 24
/** The grid's columns: the arrow keys move up and down by this many. */
const COLUMNS = 4

export interface ColourPickerProps {
  /** The parameter's label, as the customer reads it. */
  label: string
  /** The colours the customer may pick for this part. */
  filaments: readonly Filament[]
  /** The filament the customer picked, by id; undefined while the shop's choice stands. */
  value: string | undefined
  /** The shop's choice, shown while the customer has picked none. */
  fallback?: Filament
  onChange: (next: Filament | undefined) => void
}

/**
 * #6152 — one colour part of a design: a row with the colour on it, which opens a grid of
 * swatches. The material is a filter and a tag, so the same colour in PLA and PETG are
 * two choices; two filaments of one name are told apart by their hex. No support
 * material is ever offered. Styled with the theme's tokens only.
 */
export function ColourPicker({ label, filaments, value, fallback, onChange }: ColourPickerProps) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const offered = useMemo(() => customerFilaments(filaments), [filaments])
  const labels = useMemo(() => filamentLabels(offered), [offered])
  const materials = useMemo(() => [...new Set(offered.map((filament) => filament.material))].sort(), [offered])
  const picked = offered.find((filament) => filament.id === value)
  const shown = picked ?? fallback
  const [material, setMaterial] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const grid = useRef<HTMLDivElement>(null)

  const visible = offered.filter(
    (filament) =>
      (material === null || filament.material === material) &&
      (search.trim() === '' || `${labels.get(filament.id)} ${filament.material}`.toLowerCase().includes(search.trim().toLowerCase())),
  )

  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const steps: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: COLUMNS, ArrowUp: -COLUMNS }
    const step = steps[event.key]
    if (step === undefined) return
    const swatches = Array.from(grid.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? [])
    const at = swatches.indexOf(document.activeElement as HTMLButtonElement)
    const next = swatches[Math.min(swatches.length - 1, Math.max(0, (at < 0 ? 0 : at) + (at < 0 ? 0 : step)))]
    if (next) {
      event.preventDefault()
      next.focus()
    }
  }

  return (
    <div className="min-w-0">
      <p className="mb-1.5 block p-0 font-semibold" id={`${id}-label`}>
        {label}
      </p>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          type="button"
          aria-labelledby={`${id}-label ${id}-value`}
          className="flex min-h-12 w-full items-center gap-3 rounded-[10px] border-[1.5px] border-input bg-background px-3.5 py-2 text-left"
        >
          <span className="size-7 flex-none rounded-full border border-foreground/25" style={{ background: shown?.colour ?? 'transparent' }} aria-hidden="true" />
          <span id={`${id}-value`} className="min-w-0 flex-1 [overflow-wrap:anywhere]">
            {shown ? labels.get(shown.id) ?? shown.colour : 'Choose a colour'}
          </span>
          {shown && <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">{shown.material}</span>}
        </PopoverTrigger>
        <PopoverContent className="w-[min(22rem,calc(100vw-32px))]" align="start" aria-label={`Colours for ${label}`}>
          {materials.length > 1 && (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Material">
              {[null, ...materials].map((each) => (
                <button
                  key={each ?? 'all'}
                  type="button"
                  aria-pressed={material === each}
                  onClick={() => setMaterial(each)}
                  className="min-h-9 rounded-full border border-input px-3 text-sm aria-pressed:border-primary aria-pressed:bg-primary aria-pressed:text-primary-foreground"
                >
                  {each ?? 'All'}
                </button>
              ))}
            </div>
          )}
          {offered.length > SEARCH_FROM && (
            <input
              type="search"
              aria-label="Find a colour"
              placeholder="Find a colour"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className="min-h-10 w-full rounded-md border border-input bg-background px-3"
            />
          )}
          <div
            ref={grid}
            role="radiogroup"
            aria-label={label}
            onKeyDown={move}
            className="grid max-h-[50vh] grid-cols-4 gap-2 overflow-y-auto p-0.5"
          >
            {visible.map((filament) => {
              const checked = filament.id === picked?.id
              return (
                <button
                  key={filament.id}
                  type="button"
                  role="radio"
                  aria-checked={checked}
                  aria-label={`${labels.get(filament.id)} ${filament.material}`}
                  tabIndex={checked || (!picked && filament === visible[0]) ? 0 : -1}
                  onClick={() => {
                    onChange(checked ? undefined : filament)
                    setOpen(false)
                  }}
                  className="flex min-w-0 flex-col items-center gap-1 rounded-md p-1 text-center text-xs aria-checked:ring-2 aria-checked:ring-primary"
                >
                  <span className="size-10 rounded-full border border-foreground/25" style={{ background: filament.colour }} aria-hidden="true" />
                  <span className="w-full leading-tight [overflow-wrap:anywhere]">{labels.get(filament.id)}</span>
                  {materials.length > 1 && <span className="text-[10px] text-muted-foreground">{filament.material}</span>}
                </button>
              )
            })}
            {visible.length === 0 && <p className="col-span-4 m-0 text-sm text-muted-foreground">No colour matches.</p>}
          </div>
          {picked && (
            <button
              type="button"
              className="self-start text-sm underline"
              onClick={() => {
                onChange(undefined)
                setOpen(false)
              }}
            >
              Use the shop’s choice
            </button>
          )}
        </PopoverContent>
      </Popover>
    </div>
  )
}
