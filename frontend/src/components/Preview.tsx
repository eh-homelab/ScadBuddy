import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useLoader, useThree } from '@react-three/fiber'
import { Grid, OrbitControls } from '@react-three/drei'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import * as THREE from 'three'
import type { Bbox, Job } from '../api/types'
import { formatBbox } from '../lib/format'
import { Spinner } from './ui/Spinner'

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

/** Bambu X1C / P1S build plate. */
export const PLATE_MM = 256

export interface PreviewCapture {
  capturePng: () => Promise<Blob | null>
}

interface Props {
  job: Job | undefined
  rendering: boolean
  captureRef?: React.RefObject<PreviewCapture | null>
}

export function Preview({ job, rendering, captureRef }: Props) {
  // The last finished render stays on screen while the next one is in flight (spec §5.3).
  const [shown, setShown] = useState<{ url: string; bbox?: Bbox; colors: string[] } | undefined>()

  useEffect(() => {
    if (job?.status === 'done' && job.preview_url) {
      setShown({ url: job.preview_url, bbox: job.bbox_mm, colors: job.colors ?? [] })
    }
  }, [job])

  const theme = useViewerTheme()
  const failed = job?.status === 'failed'

  return (
    <div className="relative h-full min-h-0 w-full bg-bg">
      <Canvas
        key={theme.bg}
        data-testid="preview-canvas"
        gl={{ preserveDrawingBuffer: true, antialias: true }}
        camera={{ position: [210, 170, 230], fov: 35, near: 1, far: 4000 }}
        onCreated={({ gl }) => {
          if (captureRef) {
            captureRef.current = {
              capturePng: () =>
                new Promise((resolve) => {
                  gl.domElement.toBlob((blob) => resolve(blob), 'image/png')
                }),
            }
          }
        }}
      >
        <color attach="background" args={[theme.bg]} />
        <hemisphereLight args={['#dbe6f5', '#1a202b', 1.1]} />
        <directionalLight position={[180, 320, 140]} intensity={2.1} />
        <directionalLight position={[-220, 140, -180]} intensity={0.7} />

        <BuildPlate theme={theme} />
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

      <div className="pointer-events-none absolute inset-0 flex flex-col justify-between p-3">
        <div className="flex items-start justify-between gap-3">
          <PlateBadge />
          {rendering && (
            <span className="flex items-center gap-2 rounded-[6px] border border-line bg-surface/90 px-2.5 py-1 text-[12px] text-muted backdrop-blur-sm">
              <Spinner /> Rendering
            </span>
          )}
        </div>

        {shown?.bbox && !failed && <Dimensions bbox={shown.bbox} />}
      </div>

      {failed && <RenderError log={job.log_tail} />}

      {!shown && !failed && !rendering && (
        <p className="absolute inset-0 flex items-center justify-center text-[13px] text-faint">
          Change a parameter to render.
        </p>
      )}
    </div>
  )
}

function PlateBadge() {
  return (
    <span className="sb-num rounded-[6px] border border-line bg-surface/90 px-2 py-1 text-[11px] text-faint backdrop-blur-sm">
      {PLATE_MM} × {PLATE_MM} mm plate
    </span>
  )
}

function Dimensions({ bbox }: { bbox: Bbox }) {
  return (
    <dl
      data-testid="bbox-readout"
      className="w-fit self-start rounded-[6px] border border-line bg-surface/90 px-2.5 py-1.5 backdrop-blur-sm"
    >
      <dt className="text-[10px] tracking-wide text-faint">Bounding box</dt>
      <dd className="sb-num mt-0.5 text-[13px] text-ink">{formatBbox(bbox)}</dd>
    </dl>
  )
}

function RenderError({ log }: { log?: string }) {
  return (
    <div className="absolute inset-x-3 bottom-3 rounded-[6px] border border-warn/45 bg-surface/95 backdrop-blur-sm">
      <p className="border-b border-warn/25 px-3 py-2 text-[13px] text-warn">
        OpenSCAD could not render these parameters.
      </p>
      <pre
        data-testid="render-log"
        className="sb-num max-h-40 overflow-auto px-3 py-2 text-[11.5px] leading-relaxed whitespace-pre-wrap text-muted"
      >
        {log ?? 'No log output was captured.'}
      </pre>
    </div>
  )
}

function BuildPlate({ theme }: { theme: ViewerTheme }) {
  return (
    <group>
      <Grid
        args={[PLATE_MM, PLATE_MM]}
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
        <edgesGeometry
          args={[new THREE.BoxGeometry(PLATE_MM, 0.001, PLATE_MM)]}
          attach="geometry"
        />
        <lineBasicMaterial color={theme.edge} attach="material" />
      </lineSegments>
    </group>
  )
}

/**
 * Frames the model once, when its size is first known. After that the view is the
 * viewer's: orbiting is never yanked back by the next render.
 */
function FitCamera({ bbox }: { bbox?: Bbox }) {
  const camera = useThree((state) => state.camera)
  const controls = useThree((state) => state.controls) as { target: THREE.Vector3; update: () => void } | null
  const framed = useRef(false)

  useEffect(() => {
    if (!bbox || framed.current || !controls) return
    framed.current = true

    const span = Math.max(bbox.x, bbox.y, bbox.z, 20)
    const distance = span * 1.9 + 40
    const direction = new THREE.Vector3(0.78, 0.62, 0.86).normalize()
    camera.position.copy(direction.multiplyScalar(distance))
    controls.target.set(0, bbox.z / 2, 0)
    camera.lookAt(controls.target)
    controls.update()
  }, [bbox, camera, controls])

  return null
}

/**
 * The GLB is authored z-up in millimetres, the way OpenSCAD emits it; three.js is
 * y-up, so the whole model is rotated a quarter turn about X and dropped onto z=0.
 */
function Model({ url, bbox }: { url: string; bbox?: Bbox }) {
  const gltf = useLoader(GLTFLoader, url)
  const scene = useMemo(() => gltf.scene.clone(true), [gltf])
  const group = useRef<THREE.Group>(null)

  const edges = useMemo(() => {
    if (!bbox) return null
    return new THREE.BoxGeometry(bbox.x, bbox.y, bbox.z)
  }, [bbox])

  useEffect(() => () => edges?.dispose(), [edges])

  return (
    <group ref={group} rotation={[-Math.PI / 2, 0, 0]}>
      <primitive object={scene} />
      {edges && (
        <lineSegments position={[0, 0, bbox ? bbox.z / 2 : 0]}>
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
