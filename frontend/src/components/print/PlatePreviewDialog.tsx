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
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Slider } from '@/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
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
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      {/* The legacy dialog's wide layout: bounded by the visible height, its body scrolling
          between a fixed title and a fixed way out (#87, #1723, #2386). */}
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 p-0 sm:max-w-[1600px]"
      >
        <DialogHeader className="shrink-0 border-b px-5 py-4">
          <DialogTitle>{`${label} in 3D`}</DialogTitle>
          <DialogDescription>
            {sliced
              ? "The slicer's toolpaths, from the background slice of the choices on screen."
              : "What each part prints in, from the spools chosen. The model, not the slicer's toolpaths."}
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="flex flex-col gap-3">
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              spacing={0}
              aria-label="View"
              value={view}
              // A click on the view already shown would clear it: a view is always shown.
              onValueChange={(next) => next && setView(next as View)}
            >
              {(['colors', 'layers', 'speed', 'movement'] as const).map((option) => (
                <ToggleGroupItem
                  key={option}
                  value={option}
                  // Speed and Movement are the slice's: there is nothing to draw without one.
                  disabled={(option === 'speed' || option === 'movement') && toolpathUrl === null}
                  className="px-3"
                >
                  {VIEW_LABELS[option]}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            {toolpathUrl === null && (
              <p className="text-[12px] text-muted-foreground">
                Speed and Movement, and Layers as sliced, come from the background slice: they appear once it has
                finished for these choices.
              </p>
            )}
            <Card className="h-[min(60dvh,560px)] min-h-[240px] gap-0 py-0">
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
                    <p role="alert" className="text-[13px] text-destructive">
                      {toolpath.error}
                    </p>
                    <Button type="button" size="sm" variant="outline" onClick={retry}>
                      Retry
                    </Button>
                  </div>
                ) : (
                  <div role="status" className="flex h-full flex-col gap-3 p-4">
                    <span className="text-[13px] text-muted-foreground">Reading the slice's toolpaths…</span>
                    <Skeleton aria-hidden="true" className="min-h-0 flex-1" />
                  </div>
                ))}
            </Card>
            {path && <ToolpathLegend path={path} view={view} />}
            {path?.header.truncated && (
              <p className="text-[12px] text-muted-foreground">
                The slice is too large to draw whole: the first {layers.length} layers are shown.
              </p>
            )}
            {view === 'layers' && path && layers.length > 0 && (
              <div className="flex flex-col gap-2 text-[12px] text-muted-foreground">
                <span>
                  Layer <span className="sb-num text-foreground">{shownLayer + 1}</span> of{' '}
                  <span className="sb-num">{layers.length}</span>, at{' '}
                  <span className="sb-num text-foreground">{layers[shownLayer]!.z.toFixed(2)}</span> mm
                </span>
                <Slider
                  thumbLabel="Layer"
                  min={1}
                  max={layers.length}
                  step={1}
                  value={[shownLayer + 1]}
                  onValueChange={([next]) => next !== undefined && setLayer(next - 1)}
                />
              </div>
            )}
            {view === 'layers' && !sliced && (
              <div className="flex flex-col gap-2 text-[12px] text-muted-foreground">
                <span>
                  Up to <span className="sb-num text-foreground">{(cutAt ?? 0).toFixed(2)}</span>
                  {top !== null && (
                    <>
                      {' '}
                      of <span className="sb-num">{top.toFixed(2)}</span>
                    </>
                  )}{' '}
                  mm
                </span>
                <Slider
                  thumbLabel="Layer height"
                  min={step}
                  max={top ?? step}
                  step={step}
                  value={[cutAt ?? step]}
                  disabled={top === null}
                  onValueChange={([next]) => next !== undefined && setCut(next)}
                />
              </div>
            )}
          </div>
        </div>
        <DialogFooter className="m-0 shrink-0 flex-row flex-wrap items-center justify-end px-5 py-3">
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Chip({ colour, label }: { colour: string; label: string }) {
  return (
    <li>
      <Badge variant="outline" className="gap-1.5 font-normal text-muted-foreground">
        <span aria-hidden className="inline-block size-2.5 rounded-[2px]" style={{ background: colour }} />
        {label}
      </Badge>
    </li>
  )
}

/** What the colours of a toolpath view mean. */
function ToolpathLegend({ path, view }: { path: Toolpath; view: View }) {
  if (view === 'speed') {
    return (
      <div aria-label="Speed legend" className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <span className="sb-num">0</span>
        <span aria-hidden className="h-2.5 flex-1 rounded-full" style={{ background: SPEED_GRADIENT }} />
        <span className="sb-num">{Math.round(path.header.max_speed)} mm/s</span>
      </div>
    )
  }
  if (view === 'movement') {
    return (
      <ul aria-label="Movement legend" className="flex flex-wrap gap-2">
        <Chip colour={rgbCss(PRINT_MOVE)} label="Printing" />
        <Chip colour={rgbCss(TRAVEL_MOVE)} label="Travel" />
      </ul>
    )
  }
  if (view === 'layers') {
    return (
      <ul aria-label="Features" className="flex flex-wrap gap-2">
        {path.header.features.map((name, index) => (
          <Chip key={name} colour={rgbCss(featureColour(name, index))} label={name} />
        ))}
      </ul>
    )
  }
  return null
}
