import { useCallback, useMemo, useState } from 'react'
import type { NozzlePlan, SlicePreview, SlotChoice, SlotNeed, SpoolOption } from '../../api/types'
import { formatDuration, normalizeHex } from '../../lib/format'
import { spoolLabel } from '../../lib/filaments'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { PlateScene } from './PlateScene'
import { Swatch } from './Swatch'

function background(): string {
  return getComputedStyle(document.documentElement).getPropertyValue('--sb-bg').trim() || '#0b0e13'
}

/** jsdom and a browser with WebGL off draw no 3D; the pane still shows the rest. */
function canDraw3d(): boolean {
  return typeof window !== 'undefined' && 'WebGLRenderingContext' in window
}

const SIDE = { L: 'left', R: 'right' } as const

/**
 * A colour to try on the preview for one slot (#2169): its chip opens a popover with the
 * colour field, so trying one is a choice of its own, never the slot's spool.
 */
function TryColour({
  slotId,
  colour,
  onTry,
}: {
  slotId: number
  colour: string | null | undefined
  onTry: (colour: string) => void
}) {
  const hex = normalizeHex(colour ?? '#000000')
  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={`Try a colour for slot ${slotId}`}
              className="shrink-0 cursor-pointer rounded-[4px] outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              <Swatch colour={colour} />
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>Try a colour on the preview</TooltipContent>
      </Tooltip>
      <PopoverContent align="start" className="w-auto p-3">
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <input
            type="color"
            aria-label={`Colour for slot ${slotId}`}
            value={hex}
            onChange={(event) => onTry(event.target.value)}
            className="h-8 w-12 cursor-pointer rounded-md border bg-transparent p-0.5"
          />
          <span>
            Slot {slotId} on the preview only: <span className="sb-num text-foreground">{hex}</span>
          </span>
        </label>
      </PopoverContent>
    </Popover>
  )
}

/**
 * #2449 — where the slice departs from the plan it was made with: a planned slot the
 * sliced plate does not use (so that colour is lost), and a filament the slicer put on
 * the other nozzle. Nothing is said without a plan, or without slots read from the
 * slice.
 *
 * A dropped slot is judged only against `plateSlots`, the sliced plate's own slots, and
 * not at all when they are unknown (`null`): the slice is always of one plate, while the
 * plan and the dialog's slots may be every plate's, so a colour only another plate uses
 * would read as dropped. A slot the slice did use is on the sliced plate, so its side is
 * always checked.
 */
function sliceDepartures(
  plan: NozzlePlan | null,
  plateSlots: readonly SlotNeed[] | null,
  slice: SlicePreview | null,
  keepsSides: boolean,
): string[] {
  const read = slice?.slots ?? []
  if (!plan || read.length === 0) return []
  const sliced = new Map(read.map((entry) => [entry.slot_id, entry]))
  const onPlate = plateSlots === null ? null : new Set(plateSlots.map((slot) => slot.slot_id))
  const notes: string[] = []
  for (const planned of plan.slots ?? []) {
    const got = sliced.get(planned.slot_id)
    if (!got) {
      if (onPlate?.has(planned.slot_id)) {
        notes.push(
          `${planned.name} (slot ${planned.slot_id}) isn't used by the sliced plate, so it won't print: its parts may be too small to print.`,
        )
      }
    } else if (got.side && got.side !== planned.side) {
      // #2703: with every side on Automatic the slicer groups filaments itself to cut
      // purging. A side chosen by hand pins the whole map (#3955), so a slice that departs
      // from it is not expected: it is said plainly, with no reason given. Only in a file of
      // one plate: one of more is sliced by plate and regrouped whatever was chosen.
      const moved = planned.by_hand && keepsSides
        ? `The slicer put ${planned.name} (slot ${planned.slot_id}) on the ${SIDE[got.side]} nozzle, not the ${SIDE[planned.side]} you chose. Check the sides before printing.`
        : `The slicer put ${planned.name} (slot ${planned.slot_id}) on the ${SIDE[got.side]} nozzle, not the ${SIDE[planned.side]} ${planned.by_hand ? 'you chose' : 'as planned'}: it groups filaments itself to cut purging.`
      // #2737 review: without the Track Switch an AMS feeds one nozzle only, so a spool
      // moved off its planned side may have no path to the nozzle it was sliced for.
      notes.push(
        // Only a plan that says there is no switch warns; an older verdict without the field does not.
        plan.track_switch !== false
          ? moved
          : `${moved} Without the Filament Track Switch its AMS may not feed the ${SIDE[got.side]} nozzle: check where the spool is loaded before printing.`,
      )
    }
  }
  return notes
}

function grams(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null
  return `${value < 10 ? value.toFixed(1) : value.toFixed(0)} g`
}

type Props = {
  /** The plate's preview mesh, or null with no source yet. */
  url: string | null
  /** The plate's thumbnail. */
  thumbnailUrl: string | null
  /** Which plate is shown (#2169), or null for a file of one unnamed plate. */
  label?: string | null
  slots: readonly SlotNeed[]
  spools: readonly SpoolOption[]
  plan: readonly SlotChoice[]
  /** Design colour → chosen spool colour (`resolvedColors`). */
  colors: Map<string, string>
  /** The background slice (#2169). */
  slice: SlicePreview | null
  /** The nozzle plan the slice was made with (#2449). */
  nozzlePlan?: NozzlePlan | null
  /**
   * The sliced plate's own slots, or null when they are not known (every plate's, or
   * another plate's than the one shown): a planned slot is called dropped only against
   * these (`slicedPlateSlots`).
   */
  plateSlots?: readonly SlotNeed[] | null
  /** Whether a side chosen by hand is kept: only in a file of one plate (#3955 review). */
  keepsSides?: boolean
  stale: boolean
  slicing: boolean
  /** The native slicer's percent while it slices (plan 2026-10-16 Task 14). */
  progress?: number | null
  sliceError: string | null
}

/**
 * #2169 — the print dialog's docked pane, beside the choices (below them on a phone):
 * the plate in 3D in the chosen spools' colours, a colour to try per slot before
 * choosing a spool, the plate's thumbnail and legend, and what the background slice of
 * the choices on screen came to: grams per slot, time, filament changes and the nozzle
 * each filament went to. A result for earlier choices is marked until the new one lands.
 */
export function PreviewPane({
  url,
  thumbnailUrl,
  label = null,
  slots,
  spools,
  plan,
  colors,
  slice,
  nozzlePlan = null,
  plateSlots = null,
  keepsSides = true,
  stale,
  slicing,
  progress = null,
  sliceError,
}: Props) {
  /** Colours tried in the pane, by slot id: the preview only, never sent. */
  const [tried, setTried] = useState<Record<number, string>>({})
  const onHeight = useCallback(() => undefined, [])
  const byId = useMemo(() => new Map(spools.map((spool) => [spool.spool_id, spool])), [spools])
  const shownColors = useMemo(() => {
    const next = new Map(colors)
    for (const slot of slots) {
      const colour = tried[slot.slot_id]
      if (colour && slot.colour) next.set(normalizeHex(slot.colour), normalizeHex(colour))
    }
    return next
  }, [colors, slots, tried])
  const sliced = new Map((slice?.slots ?? []).map((entry) => [entry.slot_id, entry]))
  const [thumbFailed, setThumbFailed] = useState(false)
  const departures = useMemo(
    () => sliceDepartures(nozzlePlan, plateSlots, slice, keepsSides),
    [nozzlePlan, plateSlots, slice, keepsSides],
  )

  return (
    <aside className="flex min-w-0 flex-col gap-3" data-testid="preview-pane" aria-label="Preview">
      {label && (
        <p className="text-[13px] text-ink" data-testid="preview-label">
          <span className="text-muted-foreground">Showing</span> {label}
        </p>
      )}
      <Card className="relative h-[260px] gap-0 py-0 lg:h-[320px]" data-testid="preview-canvas">
        {url && canDraw3d() ? (
          <PlateScene url={url} colors={shownColors} cutAt={null} onHeight={onHeight} background={background()} />
        ) : thumbnailUrl && !thumbFailed ? (
          <img
            src={thumbnailUrl}
            alt="The plate"
            className="size-full object-contain"
            onError={() => setThumbFailed(true)}
          />
        ) : null}
        {thumbnailUrl && url && canDraw3d() && !thumbFailed && (
          <img
            src={thumbnailUrl}
            alt="The plate as last sliced"
            className="absolute right-2 bottom-2 size-16 rounded-md border bg-card object-contain"
            onError={() => setThumbFailed(true)}
          />
        )}
      </Card>

      <ul className="space-y-1.5" data-testid="preview-legend">
        {slots.map((slot) => {
          const spoolId = plan.find((choice) => choice.slot_id === slot.slot_id)?.spool_id
          const spool = spoolId == null ? undefined : byId.get(spoolId)
          const colour = tried[slot.slot_id] ?? spool?.colour ?? slot.colour
          const amount = grams(sliced.get(slot.slot_id)?.grams)
          return (
            <li key={slot.slot_id} className="flex min-w-0 items-center gap-2 text-[12px] text-foreground">
              <TryColour
                slotId={slot.slot_id}
                colour={colour}
                onTry={(next) => setTried((current) => ({ ...current, [slot.slot_id]: next }))}
              />
              <span className="min-w-0 flex-1 truncate">
                <span className="text-muted-foreground">Slot {slot.slot_id}:</span>{' '}
                {tried[slot.slot_id] ? 'colour tried here, not a spool' : spool ? spoolLabel(spool) : 'no spool chosen'}
              </span>
              {tried[slot.slot_id] && (
                <Button
                  type="button"
                  variant="link"
                  onClick={() =>
                    setTried((current) => {
                      const next = { ...current }
                      delete next[slot.slot_id]
                      return next
                    })
                  }
                  className="h-auto shrink-0 p-0 text-[11px] text-muted-foreground"
                >
                  Undo
                </Button>
              )}
              {/* #3500 review: a side is a claim about the sliced plate, as the moved-nozzle
                  note is, so it is hidden while stale with it; the grams only dim. */}
              {!stale && sliced.get(slot.slot_id)?.side && (
                <span className="shrink-0 text-muted-foreground" data-testid={`preview-side-${slot.slot_id}`}>
                  {sliced.get(slot.slot_id)?.side === 'L' ? 'left nozzle' : 'right nozzle'}
                </span>
              )}
              {amount && <span className={`sb-num shrink-0 text-faint ${stale ? 'opacity-60' : ''}`}>{amount}</span>}
            </li>
          )
        })}
      </ul>

      <Card size="sm" className="gap-0 text-[12.5px]" data-testid="slice-summary" aria-live="polite">
        <CardContent>
        <div className="flex items-center justify-between gap-2">
          <span className="font-medium text-foreground">Slice</span>
          {slicing ? (
            <span className="flex items-center gap-2 text-[12px] text-subtle" data-testid="slice-status">
              {slice ? 'Reslicing for your changes…' : 'Slicing…'}
              {progress != null && (
                <>
                  {` ${progress}%`}
                  <progress
                    max={100}
                    value={progress}
                    aria-label="Slicing progress"
                    className="h-1.5 w-16 overflow-hidden rounded-full bg-muted accent-primary [&::-moz-progress-bar]:bg-primary [&::-webkit-progress-bar]:bg-muted [&::-webkit-progress-value]:bg-primary"
                    data-testid="slice-progress"
                  />
                </>
              )}
            </span>
          ) : stale ? (
            <span className="text-[12px] text-subtle" data-testid="slice-status">
              Out of date
            </span>
          ) : null}
        </div>
        {slice ? (
          <dl className={`mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 ${stale ? 'opacity-60' : ''}`} data-stale={stale}>
            {slice.print_time_seconds != null && (
              <>
                <dt className="text-subtle">Time</dt>
                <dd className="sb-num" data-testid="slice-time">
                  {formatDuration(slice.print_time_seconds)}
                </dd>
              </>
            )}
            {slice.filament_used_g != null && (
              <>
                <dt className="text-subtle">Filament</dt>
                <dd className="sb-num" data-testid="slice-grams">
                  {grams(slice.filament_used_g)}
                </dd>
              </>
            )}
            {slice.filament_changes != null && (
              <>
                <dt className="text-subtle">Filament changes (purges)</dt>
                <dd className="sb-num" data-testid="slice-changes">
                  {slice.filament_changes === 0 ? 'none' : slice.filament_changes}
                </dd>
              </>
            )}
          </dl>
        ) : (
          !slicing && !sliceError && <p className="mt-1 text-[12px] text-subtle">Sliced once the choices are made.</p>
        )}
        {/* #3208: hidden while stale, where the grams only dim: the next slice may
            contradict them. */}
        {!stale && departures.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-[12px] text-warn" data-testid="slice-departures">
            {departures.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        )}
        {sliceError && (
          <p role="alert" className="mt-1 text-[12px] text-warn" data-testid="slice-error">
            {sliceError}
          </p>
        )}
        </CardContent>
      </Card>
    </aside>
  )
}
