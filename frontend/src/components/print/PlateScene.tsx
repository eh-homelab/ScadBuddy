import { Suspense, useEffect, useMemo } from 'react'
import { Canvas, useLoader, useThree } from '@react-three/fiber'
import { OrbitControls } from '@react-three/drei'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import * as THREE from 'three'
import { normalizeHex } from '../../lib/format'
import { cameraFraming } from '../../lib/previewFrame'
import { ErrorBoundary } from '../ErrorBoundary'

type Props = {
  url: string
  /** Design colour → the chosen spool's colour (`resolvedColors`); unmapped parts keep theirs. */
  colors: Map<string, string>
  /** Layers view: the height, in mm above the plate, the model is cut at; `null` shows it whole. */
  cutAt: number | null
  /** The model's height in mm, once it has loaded: the Layers slider's range. */
  onHeight: (height: number) => void
  background: string
}

/**
 * #1723 — the plate as it will print: its preview mesh with each part in the colour of
 * the spool chosen for it, optionally cut at a height to show it building up. The mesh
 * is the model, not the slicer's toolpaths: no infill, supports or speeds.
 */
export function PlateScene({ url, colors, cutAt, onHeight, background }: Props) {
  return (
    <ErrorBoundary
      resetKey={url}
      onError={() => useLoader.clear(GLTFLoader, url)}
      fallback={() => (
        <p role="alert" className="p-4 text-[13px] text-warn">
          The plate's preview could not be loaded.
        </p>
      )}
    >
      <Canvas
        data-testid="plate-scene"
        frameloop="demand"
        gl={{ antialias: true, localClippingEnabled: true }}
        camera={{ position: [210, 170, 230], fov: 35, near: 1, far: 4000 }}
      >
        <color attach="background" args={[background]} />
        <hemisphereLight args={['#dbe6f5', '#1a202b', 1.1]} />
        <directionalLight position={[180, 320, 140]} intensity={2.1} />
        <directionalLight position={[-220, 140, -180]} intensity={0.7} />
        <Suspense fallback={null}>
          <PlateModel url={url} colors={colors} cutAt={cutAt} onHeight={onHeight} />
        </Suspense>
        <OrbitControls makeDefault enablePan minDistance={20} maxDistance={1200} />
      </Canvas>
    </ErrorBoundary>
  )
}

function PlateModel({ url, colors, cutAt, onHeight }: Omit<Props, 'background'>) {
  const gltf = useLoader(GLTFLoader, url)
  const camera = useThree((state) => state.camera)
  const controls = useThree((state) => state.controls) as { target: THREE.Vector3; update: () => void } | null
  const invalidate = useThree((state) => state.invalidate)
  const aspect = useThree((state) => state.size.width / Math.max(1, state.size.height))

  // A copy, centred on the plate and standing on it, with materials of its own to recolour.
  const { scene, size } = useMemo(() => {
    const copy = gltf.scene.clone(true)
    const box = new THREE.Box3().setFromObject(copy)
    const center = box.getCenter(new THREE.Vector3())
    copy.position.set(-center.x, -box.min.y, -center.z)
    copy.traverse((node) => {
      const mesh = node as THREE.Mesh
      if (!mesh.isMesh) return
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
      const owned = materials.map((material) => {
        const own = material.clone() as THREE.MeshStandardMaterial
        own.userData.designColor = normalizeHex(`#${own.color.getHexString()}`)
        own.side = THREE.DoubleSide
        return own
      })
      mesh.material = Array.isArray(mesh.material) ? owned : owned[0]!
    })
    // Y-up: x wide, y tall, z deep. The framing reads (wide, deep, tall).
    const extent = box.getSize(new THREE.Vector3())
    return { scene: copy, size: [extent.x, extent.z, extent.y] as [number, number, number] }
  }, [gltf])

  useEffect(() => onHeight(size[2]), [onHeight, size])

  useEffect(() => {
    scene.traverse((node) => {
      const mesh = node as THREE.Mesh
      if (!mesh.isMesh) return
      for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
        const own = material as THREE.MeshStandardMaterial
        own.color.set(colors.get(own.userData.designColor as string) ?? (own.userData.designColor as string))
      }
    })
    invalidate()
  }, [scene, colors, invalidate])

  useEffect(() => {
    // Clipping keeps what is below the plane: everything under `cutAt` stays drawn.
    const plane = cutAt === null ? null : new THREE.Plane(new THREE.Vector3(0, -1, 0), cutAt)
    scene.traverse((node) => {
      const mesh = node as THREE.Mesh
      if (!mesh.isMesh) return
      for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
        material.clippingPlanes = plane ? [plane] : []
      }
    })
    invalidate()
  }, [scene, cutAt, invalidate])

  useEffect(() => {
    if (!controls) return
    const { target, distance } = cameraFraming(size)
    // The framing is for a landscape view; a portrait one (a phone) needs to stand back
    // as far as it is narrow, or the model's sides are cut off.
    const back = distance / Math.min(1, aspect)
    camera.position.copy(new THREE.Vector3(0.78, 0.62, 0.86).normalize().multiplyScalar(back))
    controls.target.set(...target)
    camera.lookAt(controls.target)
    controls.update()
    invalidate()
  }, [size, aspect, camera, controls, invalidate])

  return <primitive object={scene} />
}
