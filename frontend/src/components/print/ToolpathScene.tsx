import { useEffect, useMemo } from 'react'
import { Canvas, useThree } from '@react-three/fiber'
import { OrbitControls } from '@react-three/drei'
import * as THREE from 'three'
import { cameraFraming } from '../../lib/previewFrame'
import { STRIDE, TRAVEL, printedUpTo, toolpathLines, type Toolpath, type ToolpathView } from '../../lib/toolpath'

type Props = {
  path: Toolpath
  view: ToolpathView
  /** Layers view: the top layer drawn, an index into the header's layers; `null` draws all. */
  layer: number | null
  background: string
}

/**
 * #2072 — the sliced plate's toolpath, as lines: by speed, travel against print moves,
 * or by feature up to a layer. Framed on the print moves, as a wipe into the purge chute
 * would otherwise pull the camera off the bed.
 */
export function ToolpathScene({ path, view, layer, background }: Props) {
  return (
    <Canvas
      data-testid="toolpath-scene"
      frameloop="demand"
      gl={{ antialias: true }}
      camera={{ position: [210, 170, 230], fov: 35, near: 1, far: 4000 }}
    >
      <color attach="background" args={[background]} />
      <ToolpathLines path={path} view={view} layer={layer} />
      <OrbitControls makeDefault enablePan minDistance={20} maxDistance={1200} />
    </Canvas>
  )
}

/** The print moves' box, in the scene's frame: its centre and (wide, deep, tall). */
export function printBox(path: Toolpath): { center: THREE.Vector3; size: [number, number, number] } {
  const box = new THREE.Box3()
  const point = new THREE.Vector3()
  const { segments, header } = path
  for (let i = 0; i < header.segment_count; i++) {
    const at = i * STRIDE
    if (segments[at + 7] === TRAVEL) continue
    box.expandByPoint(point.set(segments[at]!, segments[at + 2]!, -segments[at + 1]!))
    box.expandByPoint(point.set(segments[at + 3]!, segments[at + 5]!, -segments[at + 4]!))
  }
  if (box.isEmpty()) return { center: new THREE.Vector3(), size: [1, 1, 1] }
  const extent = box.getSize(new THREE.Vector3())
  const center = box.getCenter(new THREE.Vector3())
  center.y = box.min.y
  return { center, size: [extent.x, extent.z, extent.y] }
}

function ToolpathLines({ path, view, layer }: Omit<Props, 'background'>) {
  const camera = useThree((state) => state.camera)
  const controls = useThree((state) => state.controls) as { target: THREE.Vector3; update: () => void } | null
  const invalidate = useThree((state) => state.invalidate)
  const aspect = useThree((state) => state.size.width / Math.max(1, state.size.height))
  const { center, size } = useMemo(() => printBox(path), [path])

  // Built once per path and view: Layers draws every layer's lines and shows the ones up
  // to the slider's by a draw range, so a slider move allocates nothing.
  const geometry = useMemo(() => {
    const { positions, colours } = toolpathLines(path, view, null)
    const built = new THREE.BufferGeometry()
    built.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    built.setAttribute('color', new THREE.BufferAttribute(colours, 3))
    return built
  }, [path, view])
  const upTo = useMemo(() => (view === 'layers' ? printedUpTo(path) : null), [path, view])
  useEffect(() => () => geometry.dispose(), [geometry])
  useEffect(() => {
    const lines = upTo === null || layer === null ? undefined : upTo[Math.min(layer, upTo.length - 1)]
    // Two vertices a line.
    geometry.setDrawRange(0, lines === undefined ? Infinity : 2 * lines)
    invalidate()
  }, [geometry, upTo, layer, invalidate])

  useEffect(() => {
    if (!controls) return
    const { target, distance } = cameraFraming(size)
    const back = distance / Math.min(1, aspect)
    camera.position.copy(new THREE.Vector3(0.78, 0.62, 0.86).normalize().multiplyScalar(back))
    controls.target.set(...target)
    camera.lookAt(controls.target)
    controls.update()
    invalidate()
  }, [size, aspect, camera, controls, invalidate])

  return (
    <lineSegments geometry={geometry} position={[-center.x, -center.y, -center.z]}>
      <lineBasicMaterial vertexColors />
    </lineSegments>
  )
}
