import { useCallback, useEffect, useRef, useState } from 'react'
import {
  PRINT_MOVE,
  SPEED_GRADIENT,
  TRAVEL_MOVE,
  featureColour,
  fetchToolpath,
  rgbCss,
  type Toolpath,
} from '../../lib/toolpath'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'
import { PlateScene } from './PlateScene'
import { ToolpathScene } from './ToolpathScene'

type View = 'colors' | 'layers' | 'speed' | 'movement'

const VIEW_LABELS: Record<View, string> = {
  colors: 'Colors',
  layers: 'Layers',
  speed: 'Speed',
  movement: 'Movement',
}

type Props = {
  open: boolean
  onClose: () => void
  /** The plate's preview mesh. */
  url: string
  /** What the dialog calls the plate: its name, or "Plate N". */
  label: string
  colors: Map<string, string>
  /**
   * #2072 — the background slice's toolpath for this plate, once it has finished: Speed
   * and Movement draw it, and Layers then shows the slice's own layers. `null` while
   * there is none (still slicing, or a slice of other choices).
   */
  toolpathUrl?: string | null
}

type Loaded = { url: string; path: Toolpath } | { url: string; error: string; attempt: number }

/** The toolpath at `url`, read once a view needs it. */
function useToolpath(url: string | null): {
  toolpath: Toolpath | 'loading' | { error: string } | null
  retry: () => void
} {
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [attempt, setAttempt] = useState(0)
  // A toolpath already read is drawn again without a second read when a view comes back.
  const kept = useRef<Loaded | null>(null)
  useEffect(() => {
    kept.current = loaded
  }, [loaded])
  useEffect(() => {
    if (url === null) return
    const had = kept.current
    if (had?.url === url && 'path' in had) return
    const abort = new AbortController()
    fetchToolpath(url, abort.signal).then(
      (path) => setLoaded({ url, path }),
      (cause: unknown) => {
        if (!abort.signal.aborted) {
          const error = cause instanceof Error ? cause.message : 'The toolpath could not be read.'
          setLoaded({ url, error, attempt })
        }
      },
    )
    return () => abort.abort()
  }, [url, attempt])
  const retry = useCallback(() => setAttempt((count) => count + 1), [])
  // A failure is forgotten once its view is left: coming back reads again, and shows as
  // loading meanwhile, not the old error with its Retry (state from the last render).
  const [shownUrl, setShownUrl] = useState(url)
  if (url !== shownUrl) {
    setShownUrl(url)
    if (url === null && loaded !== null && 'error' in loaded) setLoaded(null)
  }
  if (url === null) return { toolpath: null, retry }
  // A failure is shown only for the attempt it was of: a Retry reads as loading again.
  if (loaded?.url !== url || ('error' in loaded && loaded.attempt !== attempt)) return { toolpath: 'loading', retry }
  return { toolpath: 'path' in loaded ? loaded.path : { error: loaded.error }, retry }
}

/** The Layers slider's step, in mm: a common layer height. The cut is the model's, not a slice's. */
const STEP = 0.2

function background(): string {
  return getComputedStyle(document.documentElement).getPropertyValue('--sb-bg').trim() || '#0b0e13'
}

/**
 * #1723 — the plate in 3D, over the print dialog: Colors (each part in its chosen spool's
 * colour) by default, and Layers, the model cut at a height to show it building up. A
 * dialog of its own, so the print dialog underneath keeps every choice while it is open,
 * and on a phone it covers the screen rather than pushing the dialog's controls aside.
 */
export function PlatePreviewDialog({ open, onClose, url, label, colors, toolpathUrl = null }: Props) {
  const [view, setView] = useState<View>('colors')
  // Speed and Movement are the slice's: one that goes away (a choice changed, so the
  // slice is stale) puts the view back on Colors, never a disabled radio left checked.
  if (toolpathUrl === null && (view === 'speed' || view === 'movement')) setView('colors')
  const [height, setHeight] = useState<number | null>(null)
  const [cut, setCut] = useState<number | null>(null)
  const [layer, setLayer] = useState<number | null>(null)
  const onHeight = useCallback((next: number) => setHeight(next), [])
  // Read only once a view draws it: Colors never does.
  const sliced = toolpathUrl !== null && view !== 'colors'
  const { toolpath, retry } = useToolpath(sliced ? toolpathUrl : null)
  const path = toolpath !== null && toolpath !== 'loading' && !('error' in toolpath) ? toolpath : null

  const step = STEP
  const top = height === null ? null : Math.max(step, Math.ceil(height / step) * step)
  const cutAt = view === 'layers' && !sliced ? (cut ?? top) : null
  const layers = path?.header.layers ?? []
  const shownLayer = layer === null ? layers.length - 1 : Math.min(layer, layers.length - 1)

  return (
    <Dialog
      open={open}
      title={`${label} in 3D`}
      description={
        sliced
          ? "The slicer's toolpaths, from the background slice of the choices on screen."
          : "What each part prints in, from the spools chosen. The model, not the slicer's toolpaths."
      }
      onClose={onClose}
      size="wide"
      footer={<Button onClick={onClose}>Close</Button>}
    >
      <div className="flex flex-col gap-3">
        <div role="radiogroup" aria-label="View" className="flex gap-1.5">
          {(['colors', 'layers', 'speed', 'movement'] as const).map((option) => {
            // Speed and Movement are the slice's: there is nothing to draw without one.
            const needsSlice = option === 'speed' || option === 'movement'
            return (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={view === option}
                disabled={needsSlice && toolpathUrl === null}
                title={needsSlice && toolpathUrl === null ? 'Once the background slice has finished' : undefined}
                onClick={() => setView(option)}
                className={`rounded-[6px] border px-3 py-1 text-[13px] disabled:opacity-50 ${
                  view === option ? 'border-accent bg-accent/8 text-ink' : 'border-line text-muted'
                }`}
              >
                {VIEW_LABELS[option]}
              </button>
            )
          })}
        </div>
        {toolpathUrl === null && (
          <p className="text-[12px] text-muted">
            Speed and Movement, and Layers as sliced, come from the background slice: they appear once it has
            finished for these choices.
          </p>
        )}
        <div className="h-[min(60dvh,560px)] min-h-[240px] overflow-hidden rounded-[6px] border border-line">
          {open &&
            (!sliced ? (
              <PlateScene url={url} colors={colors} cutAt={cutAt} onHeight={onHeight} background={background()} />
            ) : path ? (
              <ToolpathScene
                path={path}
                view={view as 'layers' | 'speed' | 'movement'}
                layer={view === 'layers' ? shownLayer : null}
                background={background()}
              />
            ) : toolpath !== null && toolpath !== 'loading' && 'error' in toolpath ? (
              <div className="space-y-2 p-4">
                <p role="alert" className="text-[13px] text-warn">
                  {toolpath.error}
                </p>
                <Button size="sm" onClick={retry}>
                  Retry
                </Button>
              </div>
            ) : (
              <div className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
                <Spinner /> Reading the slice's toolpaths…
              </div>
            ))}
        </div>
        {path && <ToolpathLegend path={path} view={view} />}
        {path?.header.truncated && (
          <p className="text-[12px] text-muted">
            The slice is too large to draw whole: the first {layers.length} layers are shown.
          </p>
        )}
        {view === 'layers' && path && layers.length > 0 && (
          <label className="flex flex-col gap-1 text-[12px] text-muted">
            <span>
              Layer <span className="sb-num text-ink">{shownLayer + 1}</span> of{' '}
              <span className="sb-num">{layers.length}</span>, at{' '}
              <span className="sb-num text-ink">{layers[shownLayer]!.z.toFixed(2)}</span> mm
            </span>
            <input
              type="range"
              aria-label="Layer"
              min={1}
              max={layers.length}
              step={1}
              value={shownLayer + 1}
              onChange={(event) => setLayer(Number(event.target.value) - 1)}
              className="accent-[var(--sb-accent)]"
            />
          </label>
        )}
        {view === 'layers' && !sliced && (
          <label className="flex flex-col gap-1 text-[12px] text-muted">
            <span>
              Up to <span className="sb-num text-ink">{(cutAt ?? 0).toFixed(2)}</span>
              {top !== null && (
                <>
                  {' '}
                  of <span className="sb-num">{top.toFixed(2)}</span>
                </>
              )}{' '}
              mm
            </span>
            <input
              type="range"
              aria-label="Layer height"
              min={step}
              max={top ?? step}
              step={step}
              value={cutAt ?? step}
              disabled={top === null}
              onChange={(event) => setCut(Number(event.target.value))}
              className="accent-[var(--sb-accent)]"
            />
          </label>
        )}
      </div>
    </Dialog>
  )
}

function Chip({ colour, label }: { colour: string; label: string }) {
  return (
    <li className="flex items-center gap-1.5">
      <span aria-hidden className="inline-block size-3 rounded-[2px]" style={{ background: colour }} />
      {label}
    </li>
  )
}

/** What the colours of a toolpath view mean. */
function ToolpathLegend({ path, view }: { path: Toolpath; view: View }) {
  if (view === 'speed') {
    return (
      <div aria-label="Speed legend" className="flex items-center gap-2 text-[12px] text-muted">
        <span className="sb-num">0</span>
        <span aria-hidden className="h-2.5 flex-1 rounded-full" style={{ background: SPEED_GRADIENT }} />
        <span className="sb-num">{Math.round(path.header.max_speed)} mm/s</span>
      </div>
    )
  }
  if (view === 'movement') {
    return (
      <ul aria-label="Movement legend" className="flex gap-4 text-[12px] text-muted">
        <Chip colour={rgbCss(PRINT_MOVE)} label="Printing" />
        <Chip colour={rgbCss(TRAVEL_MOVE)} label="Travel" />
      </ul>
    )
  }
  if (view === 'layers') {
    return (
      <ul aria-label="Features" className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted">
        {path.header.features.map((name, index) => (
          <Chip key={name} colour={rgbCss(featureColour(name, index))} label={name} />
        ))}
      </ul>
    )
  }
  return null
}
