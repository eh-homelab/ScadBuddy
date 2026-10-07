import {
  Suspense,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from 'react'
import { Canvas, useLoader, useThree } from '@react-three/fiber'
import { Grid, OrbitControls } from '@react-three/drei'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import * as THREE from 'three'
import type { BoundingBox, Diagnostic, Job, Plate } from '../api/types'
import { formatBbox } from '../lib/format'
import { cameraFraming, framingSpan, sceneOffset, shouldRefit } from '../lib/previewFrame'
import type { CameraView } from '../lib/framing'
import { BBOX_OBJECT, captureSnapshot, PLATE_OBJECT, type SnapshotOptions } from '../lib/snapshot'
import { plateSize, useDisplayUnit } from '../lib/units'
import { ErrorBoundary } from './ErrorBoundary'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'
import type { RenderStage } from '../lib/useRenderJob'

const STAGE_LABELS: Record<RenderStage, string> = {
  source: 'reading the model',
  render: 'running OpenSCAD',
  split: 'colouring the preview',
  solids: 'building each colour',
  thumbnail: 'drawing the covers',
  write: 'writing the 3MF',
}

/**
 * #1743 — below this the readouts shrink to one-line chips and the notes and warnings
 * fold into one: the preview can be short on any screen (beside the assistant, inside
 * a template's own UI), so it is the preview's own size, not the window's.
 */
const COMPACT_HEIGHT = 200
const COMPACT_WIDTH = 360

/** The element's size in CSS pixels, kept current; `null` until it has been laid out. */
function useBoxSize(ref: RefObject<HTMLElement | null>): { width: number; height: number } | null {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null)
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    const measure = () => {
      const { width, height } = element.getBoundingClientRect()
      const next = width === 0 && height === 0 ? null : { width, height }
      setSize((prev) => (prev?.width === next?.width && prev?.height === next?.height ? prev : next))
    }
    measure()
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null
    observer?.observe(element)
    window.addEventListener('resize', measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [ref])
  return size
}

interface ViewerTheme {
  bg: string
  cell: string
  section: string
  edge: string
}

function readViewerTheme(): ViewerTheme {
  const style = getComputedStyle(document.documentElement)
  const read = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback
  return {
    bg: read('--sb-bg', '#0b0e13'),
    cell: read('--sb-plate-cell', '#29323f'),
    section: read('--sb-plate-section', '#3c4a5c'),
    edge: read('--sb-plate-edge', '#4c5b70'),
  }
}

/** The scene cannot use Tailwind utilities, so it reads the same CSS variables. */
function useViewerTheme(): ViewerTheme {
  const [theme, setTheme] = useState<ViewerTheme>(readViewerTheme)

  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: light)')
    const sync = () => setTheme(readViewerTheme())
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])

  return theme
}

export interface PreviewCapture {
  capturePng: () => Promise<Blob | null>
  /** A shareable image of the current view, drawn larger and without the viewer's aids. */
  captureImage: (options: SnapshotOptions) => Promise<Blob | null>
  /** The view's size in CSS pixels, which `captureImage` scales. */
  viewSize: () => { width: number; height: number }
  /** #722 — the camera's pose now, as a copy an image can be framed from. */
  cameraView: () => CameraView
}

interface Props {
  job: Job | undefined
  rendering: boolean
  /** #267 — what the render is doing, when known. */
  stage?: RenderStage | undefined
  /** #81 — the chosen printer's plate, or the configured default. Undrawn until known. */
  plate?: Plate
  captureRef?: React.RefObject<PreviewCapture | null>
  /** Laid over the scene's top left, before the plate: the page's own buttons. */
  leading?: ReactNode
  /** Laid over the scene's top right: the page's own buttons. */
  controls?: ReactNode
  /**
   * How much of the scene's left edge the page covers, as a CSS length — the
   * parameters flyout in full screen. The readouts keep clear of it; the scene does not
   * move, so the camera's view stays as it was.
   */
  covered?: string
  /**
   * #367 — the server refused the render request (a 422), and the page says why. The
   * scene then has nothing to show, but "Change a parameter to render." would read as
   * if nothing were wrong.
   */
  rejected?: boolean
  /** #937 — where OpenSCAD's warnings point the reader to fix them: the source editor. */
  sourceLink?: ReactNode
}

export function Preview({
  job,
  rendering,
  stage,
  plate,
  captureRef,
  leading,
  controls,
  covered,
  rejected = false,
  sourceLink,
}: Props) {
  // The last finished render stays on screen while the next one is in flight (spec §5.3).
  // Its notes and warnings travel with it: they explain the model on screen, not the
  // one rendering.
  const [shown, setShown] = useState<
    | {
        url: string
        bbox?: BoundingBox
        colors: string[]
        notes: string[]
        warnings: string[]
        diagnostics: Diagnostic[]
        plates: number
      }
    | undefined
  >()

  useEffect(() => {
    if (job?.status === 'done' && job.preview_url) {
      setShown({
        url: job.preview_url,
        bbox: job.bbox_mm ?? undefined,
        colors: job.colors ?? [],
        notes: job.notes ?? [],
        warnings: job.warnings ?? [],
        // A trace only says where an error was called from; a render that finished
        // has its warnings to show, which OpenSCAD logs and goes on past (#937).
        diagnostics: (job.diagnostics ?? []).filter((d) => d.severity !== 'trace'),
        plates: Math.max(job.plates?.length ?? 0, 1),
      })
    }
  }, [job])

  // #364 — the GLB that has been drawn, or failed to load: until one of them is the
  // shown one, the render is not on screen yet, so the spinner stays.
  const [loadedUrl, setLoadedUrl] = useState<string>()
  const [brokenUrl, setBrokenUrl] = useState<string>()
  const loading = shown !== undefined && loadedUrl !== shown.url && brokenUrl !== shown.url

  const theme = useViewerTheme()
  const cancelled = job?.status === 'cancelled'
  // A cancelled job gets its own copy in `RenderError` (nothing was wrong with the
  // parameters), but is otherwise gated the same as a failure: the success overlay
  // and the "change a parameter" placeholder both stay hidden.
  const failed = job?.status === 'failed' || cancelled
  const clear = covered ? { left: covered } : undefined

  const overlayRef = useRef<HTMLDivElement>(null)
  const size = useBoxSize(overlayRef)
  const compact = size !== null && (size.height < COMPACT_HEIGHT || size.width < COMPACT_WIDTH)
  const messages =
    shown && !failed && shown.warnings.length + shown.diagnostics.length + shown.notes.length > 0
      ? shown
      : undefined
  // #1744 — the panels together take at most a third of the preview's height. They are
  // laid out open once for each render and size, and fold into one chip when they would
  // take more: shown open they never scroll, so they need no pointer input, and a drag
  // that starts on one still orbits the model.
  const room = size ? Math.floor(size.height / 3) : Infinity
  const stackRef = useRef<HTMLDivElement>(null)
  const [fit, setFit] = useState<{ shown: object; width?: number; room: number; fits: boolean }>()
  const measured =
    fit !== undefined && fit.shown === messages && fit.width === size?.width && fit.room === room
  const folded = compact || (measured && !fit.fits)
  useLayoutEffect(() => {
    if (!messages || compact || measured || !stackRef.current) return
    setFit({ shown: messages, width: size?.width, room, fits: stackRef.current.scrollHeight <= room })
  }, [messages, compact, measured, size?.width, room])

  return (
    <div
      data-testid="preview"
      // min-w-0: the canvas is sized in pixels, and without it that width holds the
      // column open, so the view never narrows again after full screen or a smaller
      // window. overflow-hidden (#1743): the readouts never paint over the page around
      // a short preview.
      className="relative h-full min-h-0 w-full min-w-0 overflow-hidden bg-bg"
    >
      {/* #361 — a GLB that fails to load throws out of the scene: without this, the
          whole app unmounts. A new render, or Try again, mounts the scene afresh. */}
      <ErrorBoundary
        resetKey={shown?.url}
        // The loader keeps a failed load cached, so any remount would only rethrow it:
        // drop it as soon as it fails, and the next load of that URL fetches again.
        onError={() => {
          if (!shown) return
          useLoader.clear(GLTFLoader, shown.url)
          setBrokenUrl(shown.url)
        }}
        fallback={(_, retry) => <PreviewFailed captureRef={captureRef} onRetry={retry} />}
      >
        <Canvas
          key={theme.bg}
          data-testid="preview-canvas"
          // #364 — draw only when something changed (the controls, a new model), not
          // every frame: an idle page otherwise keeps the GPU busy.
          frameloop="demand"
          gl={{ preserveDrawingBuffer: true, antialias: true }}
          camera={{ position: [210, 170, 230], fov: 35, near: 1, far: 4000 }}
          onCreated={({ gl, get }) => {
            if (captureRef) {
              captureRef.current = {
                capturePng: () =>
                  new Promise((resolve) => {
                    gl.domElement.toBlob((blob) => resolve(blob), 'image/png')
                  }),
                // Read at capture time: the camera and scene are the ones on screen then.
                captureImage: (options) => {
                  const { scene, camera } = get()
                  return captureSnapshot(gl, scene, camera, options)
                },
                viewSize: () => ({
                  width: gl.domElement.clientWidth,
                  height: gl.domElement.clientHeight,
                }),
                cameraView: () => {
                  const { camera, controls } = get()
                  const target =
                    (controls as { target?: THREE.Vector3 } | null)?.target ?? new THREE.Vector3(0, 20, 0)
                  return {
                    position: camera.position.toArray(),
                    target: target.toArray(),
                    fov: (camera as THREE.PerspectiveCamera).fov ?? 35,
                  }
                },
              }
            }
          }}
        >
          <color attach="background" args={[theme.bg]} />
          <hemisphereLight args={['#dbe6f5', '#1a202b', 1.1]} />
          <directionalLight position={[180, 320, 140]} intensity={2.1} />
          <directionalLight position={[-220, 140, -180]} intensity={0.7} />

          {plate && <BuildPlate theme={theme} size={plate.size} />}
          <FitCamera bbox={shown?.bbox} />

          {shown && (
            <Suspense fallback={null}>
              {/* #364 — a failed render's outline would describe a model that is not
                  what the parameters now give. */}
              <Model url={shown.url} bbox={shown.bbox} outline={!failed} onLoaded={setLoadedUrl} />
            </Suspense>
          )}

          <OrbitControls
            makeDefault
            enablePan
            minDistance={40}
            maxDistance={1200}
            maxPolarAngle={Math.PI / 2 - 0.02}
            target={[0, 20, 0]}
          />
        </Canvas>
      </ErrorBoundary>

      <div
        ref={overlayRef}
        data-testid="preview-overlay"
        // A flex column, so the top row and the bottom stack never overlap: on a preview
        // too short for both, the bottom one runs past the edge and is clipped.
        className={`pointer-events-none absolute inset-0 flex flex-col justify-between ${compact ? 'gap-1 p-2' : 'p-3'}`}
        style={clear}
      >
        <div className="flex items-start justify-between gap-3">
          {/* Beside the flyout the room can run short: the left side wraps, so the
              right side's controls stay on screen. */}
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {leading}
            {plate && <PlateBadge plate={plate} compact={compact} />}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {(rendering || loading) && (
              <span
                data-overlay="chip"
                className={`flex items-center gap-2 rounded-[6px] border border-line bg-surface/90 text-muted backdrop-blur-sm ${compact ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-[12px]'}`}
              >
                <Spinner /> Rendering
                {rendering && stage && !compact && (
                  <span data-testid="render-stage">· {STAGE_LABELS[stage]}</span>
                )}
              </span>
            )}
            {controls}
          </div>
        </div>

        {!failed && (
          <div className={compact ? 'flex flex-wrap items-end gap-2' : 'flex flex-col items-start gap-2'}>
            {messages &&
              (folded ? (
                <MessagesChip
                  warnings={messages.warnings.length + messages.diagnostics.length}
                  notes={messages.notes.length}
                  compact={compact}
                >
                  <RenderMessages {...messages} sourceLink={sourceLink} overlay={false} />
                </MessagesChip>
              ) : (
                <div ref={stackRef} className="flex max-w-full flex-col items-start gap-2">
                  <RenderMessages {...messages} sourceLink={sourceLink} overlay />
                </div>
              ))}
            {shown?.bbox && <Dimensions bbox={shown.bbox} plates={shown.plates} compact={compact} />}
          </div>
        )}
      </div>

      {failed && (
        <RenderError
          cancelled={cancelled}
          log={(job.log_tail ?? []).join('\n')}
          error={job.error ?? undefined}
          warnings={job.warnings ?? []}
          covered={covered}
        />
      )}

      {!shown && !failed && !rendering && !rejected && (
        <p
          className="absolute inset-0 flex items-center justify-center text-[13px] text-faint"
          style={clear}
        >
          Change a parameter to render.
        </p>
      )}
    </div>
  )
}

function PreviewFailed({
  captureRef,
  onRetry,
}: {
  captureRef: Props['captureRef']
  onRetry: () => void
}) {
  // The scene's renderer is gone with it: a capture now would save a blank cover, so
  // the page gets none until the scene is back (its `onCreated` sets the ref again).
  useEffect(() => {
    if (captureRef) captureRef.current = null
  }, [captureRef])

  return (
    <div
      role="alert"
      data-testid="preview-failed"
      className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-[13px] text-muted"
    >
      Could not load the preview.
      <Button size="sm" onClick={onRetry}>
        Try again
      </Button>
    </div>
  )
}

function PlateBadge({ plate, compact }: { plate: Plate; compact: boolean }) {
  const unit = useDisplayUnit()
  return (
    <span
      data-testid="plate-badge"
      data-overlay="chip"
      className={`sb-num rounded-[6px] border border-line bg-surface/90 px-2 text-[11px] text-faint backdrop-blur-sm ${compact ? 'py-0.5' : 'py-1'}`}
    >
      {/* #1743 — short of room, the size alone: it is what the scene draws. */}
      {compact ? (
        plateSize(plate.size, unit)
      ) : (
        <>
          {plate.model ? `${plate.name} · ` : ''}
          {plateSize(plate.size, unit)} plate
        </>
      )}
    </span>
  )
}

function Dimensions({ bbox, plates, compact }: { bbox: BoundingBox; plates: number; compact: boolean }) {
  const unit = useDisplayUnit()
  if (compact) {
    return (
      <span
        role="group"
        aria-label="Bounding box"
        data-testid="bbox-readout"
        data-overlay="chip"
        className="sb-num rounded-[6px] border border-line bg-surface/90 px-2 py-0.5 text-[11px] text-ink backdrop-blur-sm"
      >
        {formatBbox(bbox, unit)}
        {plates > 1 && (
          <span data-testid="plate-count" className="text-faint">
            {` · ${plates} plates`}
          </span>
        )}
      </span>
    )
  }
  return (
    <dl
      data-testid="bbox-readout"
      data-overlay="chip"
      className="w-fit self-start rounded-[6px] border border-line bg-surface/90 px-2.5 py-1.5 backdrop-blur-sm"
    >
      <dt className="text-[10px] tracking-wide text-faint">Bounding box</dt>
      <dd className="sb-num mt-0.5 text-[13px] text-ink">{formatBbox(bbox, unit)}</dd>
      {/* #289 — the preview draws every plate at once; the 3MF splits them. */}
      {plates > 1 && (
        <>
          <dt className="mt-1 text-[10px] tracking-wide text-faint">Plates</dt>
          <dd data-testid="plate-count" className="sb-num mt-0.5 text-[13px] text-ink">
            {plates}, shown together
          </dd>
        </>
      )}
    </dl>
  )
}

/**
 * The top layer, where a popover escapes the preview's clipping. Without it (jsdom has
 * none) the panel is fixed-position in place, which the clipping does not reach either.
 */
const POPOVER = typeof HTMLElement !== 'undefined' && 'showPopover' in HTMLElement.prototype

const count = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`

/**
 * #1744 — the notes and warnings folded into one chip, for a preview without room to
 * show them open. It opens them in a popover in the top layer, clear of the preview's
 * clipping; Escape or a press elsewhere closes it.
 */
function MessagesChip({
  warnings,
  notes,
  compact,
  children,
}: {
  warnings: number
  notes: number
  compact: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const chip = useRef<HTMLButtonElement>(null)
  const popover = useRef<HTMLDivElement>(null)
  const id = useId()

  useLayoutEffect(() => {
    const panel = popover.current
    const anchor = chip.current
    if (!open || !panel || !anchor) return
    if (POPOVER) panel.showPopover()
    const place = () => {
      const box = anchor.getBoundingClientRect()
      const below = box.top < window.innerHeight / 2
      panel.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - panel.offsetWidth - 8))}px`
      panel.style.top = below ? `${box.bottom + 4}px` : 'auto'
      panel.style.bottom = below ? 'auto' : `${window.innerHeight - box.top + 4}px`
      panel.style.maxHeight = `${(below ? window.innerHeight - box.bottom : box.top) - 12}px`
    }
    place()
    panel.focus()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      chip.current?.focus()
    }
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node
      if (popover.current?.contains(target) || chip.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointer)
    }
  }, [open])

  return (
    <>
      <button
        ref={chip}
        type="button"
        data-overlay="chip"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((was) => !was)}
        className={`pointer-events-auto rounded-[6px] border bg-surface/90 px-2 text-[11px] backdrop-blur-sm hover:bg-surface ${compact ? 'py-0.5' : 'py-1'} ${warnings > 0 ? 'border-warn/45 text-warn' : 'border-line text-muted'}`}
      >
        {[warnings > 0 && count(warnings, 'warning'), notes > 0 && count(notes, 'note')]
          .filter(Boolean)
          .join(' · ')}
      </button>
      {open && (
        <div
          ref={popover}
          id={id}
          popover={POPOVER ? 'manual' : undefined}
          tabIndex={-1}
          role="group"
          aria-label="About this render"
          className="m-0 flex w-max max-w-[min(32rem,calc(100vw-1rem))] flex-col gap-2 overflow-auto border-0 bg-transparent p-0 outline-none"
          style={{ position: 'fixed', inset: 'auto' }}
        >
          {children}
        </div>
      )}
    </>
  )
}

/** A render's notes and warnings: ScadBuddy's own, OpenSCAD's, then the template's. */
function RenderMessages({
  warnings,
  diagnostics,
  notes,
  sourceLink,
  overlay,
}: {
  warnings: string[]
  diagnostics: Diagnostic[]
  notes: string[]
  sourceLink?: ReactNode
  overlay: boolean
}) {
  return (
    <>
      {warnings.length > 0 && <RenderWarnings warnings={warnings} overlay={overlay} />}
      {diagnostics.length > 0 && (
        <OpenScadWarnings diagnostics={diagnostics} sourceLink={sourceLink} overlay={overlay} />
      )}
      {notes.length > 0 && <RenderNotes notes={notes} overlay={overlay} />}
    </>
  )
}

/**
 * Over the scene a panel takes no pointer input, so a drag that starts on it still
 * orbits the model (#1744): it is shown open there only when it fits without scrolling.
 * In the popover it is an ordinary panel.
 */
const panelClass = (overlay: boolean, border: string) =>
  `w-fit max-w-[min(32rem,100%)] rounded-[6px] border ${border} px-2.5 py-1.5 ${overlay ? 'pointer-events-none bg-surface/90 backdrop-blur-sm' : 'bg-surface'}`

/**
 * #285 — what the template echoed as `NOTE:`/`WARNING:` on a successful render: a
 * size it capped or text it shrank to fit the plate. Without it the result simply
 * differs from the parameters, with nothing to say why.
 */
function RenderNotes({ notes, overlay }: { notes: string[]; overlay: boolean }) {
  return (
    <section
      data-testid="render-notes"
      data-overlay="panel"
      aria-label="Notes from the template"
      className={panelClass(overlay, 'border-line')}
    >
      <h3 className="text-[10px] tracking-wide text-faint">From the template</h3>
      <ul className="mt-0.5 space-y-0.5 text-[12px] leading-snug text-muted">
        {/* An index key: a display-only list, and its text need not be unique. */}
        {notes.map((note, index) => (
          <li key={index}>{note}</li>
        ))}
      </ul>
    </section>
  )
}

/**
 * #937 — what OpenSCAD logged about a render that still finished: a warning means it
 * dropped or guessed at something (a child of `cube()`, an undefined variable), so
 * the preview may be missing part of the model.
 */
function OpenScadWarnings({
  diagnostics,
  sourceLink,
  overlay,
}: {
  diagnostics: Diagnostic[]
  sourceLink?: ReactNode
  overlay: boolean
}) {
  return (
    <section
      aria-label="OpenSCAD warnings"
      data-overlay="panel"
      className={panelClass(overlay, 'border-warn/45')}
    >
      <h3 className="flex items-center gap-2 text-[10px] tracking-wide text-warn">
        From OpenSCAD
        {sourceLink && (
          <span className="pointer-events-auto text-[11px] tracking-normal underline">{sourceLink}</span>
        )}
      </h3>
      <ul className="mt-0.5 space-y-0.5 text-[12px] leading-snug text-warn">
        {diagnostics.map((diagnostic, index) => (
          <li key={index} className="flex gap-2">
            <span className="sb-num shrink-0 text-faint">
              {diagnostic.line == null
                ? ''
                : diagnostic.file && diagnostic.file !== 'model.scad'
                  ? `${diagnostic.file}:${diagnostic.line}`
                  : `Line ${diagnostic.line}`}
            </span>
            <span>{diagnostic.message}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * #383 — ScadBuddy's own warnings about a render, say a file parameter's asset
 * OpenSCAD could not open. Warn-coloured and titled for ScadBuddy, so it reads as
 * distinct from what the template itself said (`RenderNotes`).
 */
function RenderWarnings({
  warnings,
  inline = false,
  overlay = false,
}: {
  warnings: string[]
  inline?: boolean
  overlay?: boolean
}) {
  return (
    <section
      data-testid="render-warnings"
      aria-label="Render warnings"
      data-overlay={inline ? undefined : 'panel'}
      className={
        inline ? 'max-h-28 overflow-auto border-b border-warn/25 px-3 py-2' : panelClass(overlay, 'border-warn/45')
      }
    >
      <h3 className="text-[10px] tracking-wide text-warn">From ScadBuddy</h3>
      <ul className="mt-0.5 list-inside list-disc space-y-0.5 text-[12px] leading-snug text-warn">
        {warnings.map((warning, index) => (
          <li key={index}>{warning}</li>
        ))}
      </ul>
    </section>
  )
}

function RenderError({
  cancelled = false,
  log,
  error,
  warnings,
  covered,
}: {
  cancelled?: boolean
  log?: string
  error?: string
  warnings: string[]
  covered?: string
}) {
  // No OpenSCAD log: the render failed in ScadBuddy's own stages (#952), so the
  // job's error says what happened and OpenSCAD is not to blame.
  const ownFailure = !cancelled && !log && !!error
  return (
    <div
      // #1743 — no taller than the preview, which clips it: on a short one it scrolls.
      className="absolute inset-x-3 bottom-3 max-h-[calc(100%-1.5rem)] overflow-auto rounded-[6px] border border-warn/45 bg-surface/95 backdrop-blur-sm"
      style={covered ? { left: `calc(${covered} + 0.75rem)` } : undefined}
    >
      <p className="border-b border-warn/25 px-3 py-2 text-[13px] text-warn">
        {cancelled
          ? 'This render was cancelled. A newer request replaced it before it finished — your parameters were not the problem.'
          : ownFailure
            ? 'ScadBuddy could not finish this render.'
            : 'OpenSCAD could not render these parameters.'}
      </p>
      {warnings.length > 0 && <RenderWarnings warnings={warnings} inline />}
      <pre
        data-testid="render-log"
        className="sb-num max-h-40 overflow-auto px-3 py-2 text-[11.5px] leading-relaxed whitespace-pre-wrap text-muted"
      >
        {(ownFailure ? error : log) || 'No log output was captured.'}
      </pre>
    </div>
  )
}

function BuildPlate({ theme, size }: { theme: ViewerTheme; size: [number, number] }) {
  // Plate X is the scene's X and plate Y its Z: the scene is Y-up.
  const [width, depth] = size
  const outline = useMemo(() => new THREE.BoxGeometry(width, 0.001, depth), [width, depth])
  useEffect(() => () => outline.dispose(), [outline])
  return (
    <group name={PLATE_OBJECT}>
      <Grid
        args={[width, depth]}
        cellSize={10}
        cellThickness={0.6}
        cellColor={theme.cell}
        sectionSize={50}
        sectionThickness={1.1}
        sectionColor={theme.section}
        fadeDistance={1400}
        fadeStrength={1}
        infiniteGrid={false}
        position={[0, 0, 0]}
      />
      <lineSegments position={[0, 0.05, 0]}>
        <edgesGeometry args={[outline]} attach="geometry" />
        <lineBasicMaterial color={theme.edge} attach="material" />
      </lineSegments>
    </group>
  )
}

/**
 * Frames the model when its size is first known, and again when a new render's size
 * moves far from the one framed (#364: a preset can take a box from 80 to 258 mm).
 * Otherwise the view is the viewer's: a small edit never yanks an orbit back.
 */
function FitCamera({ bbox }: { bbox?: BoundingBox }) {
  const camera = useThree((state) => state.camera)
  const controls = useThree((state) => state.controls) as { target: THREE.Vector3; update: () => void } | null
  const invalidate = useThree((state) => state.invalidate)
  const framedSpan = useRef<number | null>(null)

  useEffect(() => {
    if (!bbox || !controls || !shouldRefit(framedSpan.current, bbox.size)) return
    framedSpan.current = framingSpan(bbox.size)

    const { target, distance } = cameraFraming(bbox.size)
    const direction = new THREE.Vector3(0.78, 0.62, 0.86).normalize()
    camera.position.copy(direction.multiplyScalar(distance))
    controls.target.set(...target)
    camera.lookAt(controls.target)
    controls.update()
    invalidate?.()
  }, [bbox, camera, controls, invalidate])

  return null
}

/**
 * The GLB arrives **Y-up already** — the backend's writer applies its own
 * `Z_UP_TO_Y_UP` before serialising — so it drops straight into the three.js scene.
 * An earlier version rotated it a quarter turn about X on the assumption it was
 * OpenSCAD's Z-up, which stood the model on its edge; the msw fixture happened to be
 * authored Z-up too, so every mocked test agreed with it.
 */
function Model({
  url,
  bbox,
  outline,
  onLoaded,
}: {
  url: string
  bbox?: BoundingBox
  outline: boolean
  onLoaded: (url: string) => void
}) {
  const gltf = useLoader(GLTFLoader, url)
  const scene = useMemo(() => gltf.scene.clone(true), [gltf])
  const group = useRef<THREE.Group>(null)
  // #364 — the GLB is in OpenSCAD's coordinates; the outline and the camera are
  // centred on the plate. Move the model there, as the slicer will.
  const offset = useMemo(() => (bbox ? sceneOffset(bbox) : ([0, 0, 0] as const)), [bbox])

  useEffect(() => onLoaded(url), [onLoaded, url])

  const edges = useMemo(() => {
    if (!bbox) return null
    // bbox.size is model space (wide, deep, tall); the scene is Y-up.
    const [width, depth, height] = bbox.size
    return new THREE.BoxGeometry(width, height, depth)
  }, [bbox])

  useEffect(() => () => edges?.dispose(), [edges])

  return (
    <group ref={group}>
      <group position={offset}>
        <primitive object={scene} />
      </group>
      {edges && outline && (
        <lineSegments name={BBOX_OBJECT} position={[0, bbox ? bbox.size[2] / 2 : 0, 0]}>
          <edgesGeometry args={[edges]} attach="geometry" />
          <lineBasicMaterial
            color="#f2a93b"
            transparent
            opacity={0.35}
            attach="material"
          />
        </lineSegments>
      )}
    </group>
  )
}
