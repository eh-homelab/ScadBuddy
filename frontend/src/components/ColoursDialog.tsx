import { useEffect, useState } from 'react'
import { ApiError, api, type ColourBreakdown } from '../api/client'
import { inkOn, normalizeHex } from '../lib/format'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

/** The named views the backend draws from (`ViewName`). */
const VIEWS = ['iso', 'front', 'back', 'left', 'right', 'top', 'bottom'] as const
type View = (typeof VIEWS)[number]

interface Props {
  open: boolean
  /** The finished render to draw. */
  jobId: string | undefined
  /** The job's colours in extruder order, so each tile can name its extruder. */
  colors: string[]
  onClose: () => void
}

/**
 * #1289 — the render drawn once per colour (`GET /jobs/{id}/colours.png`): on each tile
 * that colour's parts are in their colour and every other part in grey, so a letter on
 * the wrong extruder, or a colour hidden inside another, shows before printing. Each
 * tile is labelled with its extruder, and the same legend is listed below the grid.
 */
export function ColoursDialog({ open, jobId, colors, onClose }: Props) {
  const [view, setView] = useState<View>('iso')
  const key = `${jobId}:${view}`
  const [drawn, setDrawn] = useState<{ key: string; breakdown: ColourBreakdown; url: string } | null>(null)
  const [failed, setFailed] = useState<{ key: string; message: string } | null>(null)
  // The view being asked for while the dialog is open. When it changes (the dialog
  // opens again, or the view goes back to one that failed), an old failure of that
  // view is dropped here, during the render, so the retry shows as drawing from its
  // first paint; an effect would clear it only after one frame of the old alert.
  const attempt = open && jobId ? key : null
  const [asked, setAsked] = useState<string | null>(null)
  if (attempt !== asked) {
    setAsked(attempt)
    if (attempt !== null && failed?.key === attempt) setFailed(null)
  }

  useEffect(() => {
    if (!open || !jobId) return
    const stop = new AbortController()
    const want = `${jobId}:${view}`
    api.getJobColours(jobId, view, stop.signal).then(
      (breakdown) => {
        if (stop.signal.aborted) return
        setDrawn({ key: want, breakdown, url: URL.createObjectURL(breakdown.image) })
      },
      (cause: unknown) => {
        if (stop.signal.aborted) return
        setFailed({ key: want, message: cause instanceof ApiError ? cause.detail : 'The colours could not be drawn.' })
      },
    )
    return () => stop.abort()
  }, [open, jobId, view])

  // Each image URL is released when the next replaces it, and the last one on unmount.
  useEffect(() => {
    if (!drawn) return
    const { url } = drawn
    return () => URL.revokeObjectURL(url)
  }, [drawn])

  const shown = drawn?.key === key ? drawn : null
  const problem = failed?.key === key ? failed.message : null
  const extruders = colors.map(normalizeHex)

  return (
    <Dialog
      open={open}
      title="Colours"
      description="The render drawn once per colour: each tile shows where that colour goes, with everything else in grey."
      onClose={onClose}
      size="wide"
    >
      <label className="mb-3 flex items-center gap-2 text-[13px]">
        View
        <select
          value={view}
          onChange={(event) => setView(event.target.value as View)}
          className="rounded-[4px] border border-line bg-surface px-2 py-1"
        >
          {VIEWS.map((name) => (
            <option key={name} value={name}>
              {name[0]!.toUpperCase() + name.slice(1)}
            </option>
          ))}
        </select>
      </label>
      {problem ? (
        <p role="alert" className="text-[13px] text-warn">
          {problem}
        </p>
      ) : !shown ? (
        <div className="flex items-center gap-2 text-[13px] text-muted" role="status">
          <Spinner />
          Drawing each colour…
        </div>
      ) : (
        <Breakdown breakdown={shown.breakdown} url={shown.url} extruders={extruders} />
      )}
    </Dialog>
  )
}

function Breakdown({ breakdown, url, extruders }: { breakdown: ColourBreakdown; url: string; extruders: string[] }) {
  const { colours, columns } = breakdown
  const rows = Math.ceil(colours.length / columns)
  /** 1-based, or 0 for a colour the parts carry that is not one of the job's extruders. */
  const extruderOf = (hex: string) => extruders.indexOf(hex) + 1
  return (
    <>
      <div className="relative mx-auto w-full max-w-[640px]" style={{ aspectRatio: `${columns} / ${rows}` }}>
        <img src={url} alt="" className="absolute inset-0 size-full" />
        {/* The tiles, laid over the image in the backend's own grid, each named by its extruder. */}
        <ol
          aria-hidden="true"
          className="absolute inset-0 grid"
          style={{ gridTemplateColumns: `repeat(${columns}, 1fr)`, gridTemplateRows: `repeat(${rows}, 1fr)` }}
        >
          {colours.map((colour, index) => {
            const hex = normalizeHex(colour)
            const extruder = extruderOf(hex)
            return (
              <li key={`${hex}-${index}`} className="p-1">
                <span
                  style={{ background: hex, color: inkOn(hex) }}
                  className="sb-num inline-flex size-5 items-center justify-center rounded-[3px] text-[10px] font-semibold ring-1 ring-black/25 ring-inset"
                >
                  {extruder || '?'}
                </span>
              </li>
            )
          })}
        </ol>
      </div>
      <ol aria-label="Tiles, row by row" className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted">
        {colours.map((colour, index) => {
          const hex = normalizeHex(colour)
          return (
            <li key={`${hex}-${index}`} className="flex items-center gap-1.5">
              <span aria-hidden="true" style={{ background: hex }} className="size-3 rounded-[2px] ring-1 ring-black/25 ring-inset" />
              {`Tile ${index + 1}: ${extruderOf(hex) ? `Extruder ${extruderOf(hex)}` : 'Not an extruder colour'}, ${hex}`}
            </li>
          )
        })}
      </ol>
    </>
  )
}
