import { useCallback, useMemo, useState } from 'react'
import type { NozzlePlan, SlicePreview, SlotChoice, SlotNeed, SpoolOption } from '../../api/types'
import { formatDuration, normalizeHex } from '../../lib/format'
import { spoolLabel } from '../../lib/filaments'
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
 * #2449 — where the slice departs from the plan it was made with: a planned slot the
 * sliced plate does not use (its parts too fine to print, so that colour is lost), and
 * a filament the slicer put on the other nozzle. Nothing is said without a plan, or
 * without slots read from the slice.
 */
function sliceDepartures(
  plan: NozzlePlan | null,
  slots: readonly SlotNeed[],
  slice: SlicePreview | null,
): string[] {
  const read = slice?.slots ?? []
  if (!plan || read.length === 0) return []
  const sliced = new Map(read.map((entry) => [entry.slot_id, entry]))
  const onPlate = new Set(slots.map((slot) => slot.slot_id))
  const notes: string[] = []
  for (const planned of plan.slots ?? []) {
    if (!onPlate.has(planned.slot_id)) continue
    const got = sliced.get(planned.slot_id)
    if (!got) {
      notes.push(
        `${planned.name} (slot ${planned.slot_id}) isn't used by the sliced plate: its parts are too small to print, so it won't print.`,
      )
    } else if (got.side && got.side !== planned.side) {
      notes.push(
        `The slicer put ${planned.name} (slot ${planned.slot_id}) on the ${SIDE[got.side]} nozzle, not the ${SIDE[planned.side]} as planned.`,
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
  stale: boolean
  slicing: boolean
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
  stale,
  slicing,
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
  const departures = sliceDepartures(nozzlePlan, slots, slice)

  return (
    <aside className="flex min-w-0 flex-col gap-3" data-testid="preview-pane" aria-label="Preview">
      {label && (
        <p className="text-[13px] text-ink" data-testid="preview-label">
          <span className="text-muted">Showing</span> {label}
        </p>
      )}
      <div className="relative h-[260px] overflow-hidden rounded-[6px] border border-line bg-surface-2 lg:h-[320px]">
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
            className="absolute right-2 bottom-2 size-16 rounded-[4px] border border-line bg-surface object-contain"
            onError={() => setThumbFailed(true)}
          />
        )}
      </div>

      <ul className="space-y-1.5" data-testid="preview-legend">
        {slots.map((slot) => {
          const spoolId = plan.find((choice) => choice.slot_id === slot.slot_id)?.spool_id
          const spool = spoolId == null ? undefined : byId.get(spoolId)
          const colour = tried[slot.slot_id] ?? spool?.colour ?? slot.colour
          const amount = grams(sliced.get(slot.slot_id)?.grams)
          return (
            <li key={slot.slot_id} className="flex min-w-0 items-center gap-2 text-[12px] text-ink">
              <label className="relative shrink-0 cursor-pointer" title="Try a colour on the preview">
                <Swatch colour={colour} />
                <input
                  type="color"
                  aria-label={`Try a colour for slot ${slot.slot_id}`}
                  value={normalizeHex(colour ?? '#000000')}
                  onChange={(event) => setTried((current) => ({ ...current, [slot.slot_id]: event.target.value }))}
                  className="absolute inset-0 size-full cursor-pointer opacity-0"
                />
              </label>
              <span className="min-w-0 flex-1 truncate">
                <span className="text-muted">Slot {slot.slot_id}:</span>{' '}
                {tried[slot.slot_id] ? 'colour tried here, not a spool' : spool ? spoolLabel(spool) : 'no spool chosen'}
              </span>
              {tried[slot.slot_id] && (
                <button
                  type="button"
                  onClick={() =>
                    setTried((current) => {
                      const next = { ...current }
                      delete next[slot.slot_id]
                      return next
                    })
                  }
                  className="shrink-0 text-[11px] text-muted underline decoration-dotted underline-offset-2 hover:text-ink"
                >
                  Undo
                </button>
              )}
              {sliced.get(slot.slot_id)?.side && (
                <span className={`shrink-0 text-muted ${stale ? 'opacity-60' : ''}`} data-testid={`preview-side-${slot.slot_id}`}>
                  {sliced.get(slot.slot_id)?.side === 'L' ? 'left nozzle' : 'right nozzle'}
                </span>
              )}
              {amount && <span className={`sb-num shrink-0 text-faint ${stale ? 'opacity-60' : ''}`}>{amount}</span>}
            </li>
          )
        })}
      </ul>

      <div className="rounded-[6px] border border-line px-3 py-2 text-[12.5px]" data-testid="slice-summary" aria-live="polite">
        <div className="flex items-center justify-between gap-2">
          <span className="text-ink">Slice</span>
          {slicing ? (
            <span className="text-[12px] text-muted" data-testid="slice-status">
              {slice ? 'Reslicing for your changes…' : 'Slicing…'}
            </span>
          ) : stale ? (
            <span className="text-[12px] text-muted" data-testid="slice-status">
              Out of date
            </span>
          ) : null}
        </div>
        {slice ? (
          <dl className={`mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 ${stale ? 'opacity-60' : ''}`} data-stale={stale}>
            {slice.print_time_seconds != null && (
              <>
                <dt className="text-muted">Time</dt>
                <dd className="sb-num" data-testid="slice-time">
                  {formatDuration(slice.print_time_seconds)}
                </dd>
              </>
            )}
            {slice.filament_used_g != null && (
              <>
                <dt className="text-muted">Filament</dt>
                <dd className="sb-num" data-testid="slice-grams">
                  {grams(slice.filament_used_g)}
                </dd>
              </>
            )}
            {slice.filament_changes != null && (
              <>
                <dt className="text-muted">Filament changes (purges)</dt>
                <dd className="sb-num" data-testid="slice-changes">
                  {slice.filament_changes === 0 ? 'none' : slice.filament_changes}
                </dd>
              </>
            )}
          </dl>
        ) : (
          !slicing && !sliceError && <p className="mt-1 text-[12px] text-muted">Sliced once the choices are made.</p>
        )}
        {departures.length > 0 && (
          <ul className={`mt-1 space-y-0.5 text-[12px] text-warn ${stale ? 'opacity-60' : ''}`} data-testid="slice-departures">
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
      </div>
    </aside>
  )
}
