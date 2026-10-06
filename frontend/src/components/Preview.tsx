import { Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Canvas, useLoader, useThree } from '@react-three/fiber'
import { Grid, OrbitControls } from '@react-three/drei'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import * as THREE from 'three'
import type { BoundingBox, Job, Plate } from '../api/types'
import { formatBbox } from '../lib/format'
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
        plates: Math.max(job.plates?.length ?? 0, 1),
      })
    }
  }, [job])

  const theme = useViewerTheme()
  const cancelled = job?.status === 'cancelled'
  // A cancelled job gets its own copy in `RenderError` (nothing was wrong with the
  // parameters), but is otherwise gated the same as a failure: the success overlay
  // and the "change a parameter" placeholder both stay hidden.
  const failed = job?.status === 'failed' || cancelled
  const clear = covered ? { left: covered } : undefined

  return (
    <div
      // min-w-0: the canvas is sized in pixels, and without it that width holds the
      // column open, so the view never narrows again after full screen or a smaller
      // window.
      className="relative h-full min-h-0 w-full min-w-0 bg-bg"
    >
      {/* #361 — a GLB that fails to load throws out of the scene: without this, the
          whole app unmounts. A new render, or Try again, mounts the scene afresh. */}
      <ErrorBoundary
        resetKey={shown?.url}
        // The loader keeps a failed load cached, so any remount would only rethrow it:
        // drop it as soon as it fails, and the next load of that URL fetches again.
        onError={() => {
          if (shown) useLoader.clear(GLTFLoader, shown.url)
        }}
        fallback={(_, retry) => <PreviewFailed captureRef={captureRef} onRetry={retry} />}
      >
        <Canvas
          key={theme.bg}
          data-testid="preview-canvas"
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
              <Model url={shown.url} bbox={shown.bbox} />
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
        className="pointer-events-none absolute inset-0 flex flex-col justify-between p-3"
        style={clear}
      >
        <div className="flex items-start justify-between gap-3">
          {/* Beside the flyout the room can run short: the left side wraps, so the
              right side's controls stay on screen. */}
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {leading}
            {plate && <PlateBadge plate={plate} />}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {rendering && (
              <span className="flex items-center gap-2 rounded-[6px] border border-line bg-surface/90 px-2.5 py-1 text-[12px] text-muted backdrop-blur-sm">
                <Spinner /> Rendering
                {stage && <span data-testid="render-stage">· {STAGE_LABELS[stage]}</span>}
              </span>
            )}
            {controls}
          </div>
        </div>

        {!failed && (
          <div className="flex flex-col items-start gap-2">
            {shown && shown.warnings.length > 0 && <RenderWarnings warnings={shown.warnings} />}
            {shown && shown.notes.length > 0 && <RenderNotes notes={shown.notes} />}
            {shown?.bbox && <Dimensions bbox={shown.bbox} plates={shown.plates} />}
          </div>
        )}
      </div>

      {failed && (
        <RenderError
          cancelled={cancelled}
          log={(job.log_tail ?? []).join('\n')}
          warnings={job.warnings ?? []}
          covered={covered}
        />
      )}

      {!shown && !failed && !rendering && (
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

function PlateBadge({ plate }: { plate: Plate }) {
  const unit = useDisplayUnit()
  return (
    <span className="sb-num rounded-[6px] border border-line bg-surface/90 px-2 py-1 text-[11px] text-faint backdrop-blur-sm">
      {plate.model ? `${plate.name} · ` : ''}
      {plateSize(plate.size, unit)} plate
    </span>
  )
}

function Dimensions({ bbox, plates }: { bbox: BoundingBox; plates: number }) {
  const unit = useDisplayUnit()
  return (
    <dl
      data-testid="bbox-readout"
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
 * #285 — what the template echoed as `NOTE:`/`WARNING:` on a successful render: a
 * size it capped or text it shrank to fit the plate. Without it the result simply
 * differs from the parameters, with nothing to say why.
 */
export function RenderNotes({ notes }: { notes: string[] }) {
  return (
    <section
      data-testid="render-notes"
      aria-label="Notes from the template"
      className="pointer-events-auto max-h-28 w-fit max-w-[min(32rem,100%)] overflow-auto rounded-[6px] border border-line bg-surface/90 px-2.5 py-1.5 backdrop-blur-sm"
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
 * #383 — ScadBuddy's own warnings about a render, say a file parameter's asset
 * OpenSCAD could not open. Warn-coloured and titled for ScadBuddy, so it reads as
 * distinct from what the template itself said (`RenderNotes`).
 */
export function RenderWarnings({ warnings, inline = false }: { warnings: string[]; inline?: boolean }) {
  return (
    <section
      data-testid="render-warnings"
      aria-label="Render warnings"
      className={
        inline
          ? 'max-h-28 overflow-auto border-b border-warn/25 px-3 py-2'
          : 'pointer-events-auto max-h-28 w-fit max-w-[min(32rem,100%)] overflow-auto rounded-[6px] border border-warn/45 bg-surface/90 px-2.5 py-1.5 backdrop-blur-sm'
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
  warnings,
  covered,
}: {
  cancelled?: boolean
  log?: string
  warnings: string[]
  covered?: string
}) {
  return (
    <div
      className="absolute inset-x-3 bottom-3 rounded-[6px] border border-warn/45 bg-surface/95 backdrop-blur-sm"
      style={covered ? { left: `calc(${covered} + 0.75rem)` } : undefined}
    >
      <p className="border-b border-warn/25 px-3 py-2 text-[13px] text-warn">
        {cancelled
          ? 'This render was cancelled. A newer request replaced it before it finished — your parameters were not the problem.'
          : 'OpenSCAD could not render these parameters.'}
      </p>
      {warnings.length > 0 && <RenderWarnings warnings={warnings} inline />}
      <pre
        data-testid="render-log"
        className="sb-num max-h-40 overflow-auto px-3 py-2 text-[11.5px] leading-relaxed whitespace-pre-wrap text-muted"
      >
        {log ?? 'No log output was captured.'}
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
 * Frames the model once, when its size is first known. After that the view is the
 * viewer's: orbiting is never yanked back by the next render.
 */
function FitCamera({ bbox }: { bbox?: BoundingBox }) {
  const camera = useThree((state) => state.camera)
  const controls = useThree((state) => state.controls) as { target: THREE.Vector3; update: () => void } | null
  const framed = useRef(false)

  useEffect(() => {
    if (!bbox || framed.current || !controls) return
    framed.current = true

    const [width, depth, height] = bbox.size
    const span = Math.max(width, depth, height, 20)
    const distance = span * 1.9 + 40
    const direction = new THREE.Vector3(0.78, 0.62, 0.86).normalize()
    camera.position.copy(direction.multiplyScalar(distance))
    controls.target.set(0, height / 2, 0)
    camera.lookAt(controls.target)
    controls.update()
  }, [bbox, camera, controls])

  return null
}

/**
 * The GLB arrives **Y-up already** — the backend's writer applies its own
 * `Z_UP_TO_Y_UP` before serialising — so it drops straight into the three.js scene.
 * An earlier version rotated it a quarter turn about X on the assumption it was
 * OpenSCAD's Z-up, which stood the model on its edge; the msw fixture happened to be
 * authored Z-up too, so every mocked test agreed with it.
 */
function Model({ url, bbox }: { url: string; bbox?: BoundingBox }) {
  const gltf = useLoader(GLTFLoader, url)
  const scene = useMemo(() => gltf.scene.clone(true), [gltf])
  const group = useRef<THREE.Group>(null)

  const edges = useMemo(() => {
    if (!bbox) return null
    // bbox.size is model space (wide, deep, tall); the scene is Y-up.
    const [width, depth, height] = bbox.size
    return new THREE.BoxGeometry(width, height, depth)
  }, [bbox])

  useEffect(() => () => edges?.dispose(), [edges])

  return (
    <group ref={group}>
      <primitive object={scene} />
      {edges && (
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
