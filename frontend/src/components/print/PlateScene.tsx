import { Suspense, useEffect, useMemo, useState } from 'react'
import { Canvas, useLoader, useThree } from '@react-three/fiber'
import { OrbitControls } from '@react-three/drei'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import * as THREE from 'three'
import { designColorOf } from '../../lib/glbColor'
import { cameraFraming } from '../../lib/previewFrame'
import { ErrorBoundary } from '../ErrorBoundary'
import { Button } from '@/components/ui/button'

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
      fallback={(error, retry) => <PreviewFailure url={url} error={error} retry={retry} />}
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

/**
 * The status a failed load answered with, from three's loader error ("fetch for … responded
 * with 422: …", wrapped by `useLoader`), or null when it never got an answer.
 */
function failedStatus(error: Error): number | null {
  const match = /responded with (\d{3})\b/.exec(error.message)
  return match ? Number(match[1]) : null
}

/**
 * #2111 — why the preview failed. A 4xx is the backend refusing this file for good (sliced,
 * damaged, past a read budget): its problem detail says why, and Try again could never
 * succeed, so none is offered. Anything else (a network error, a 5xx) may pass, so it is.
 * The loader keeps no body, so the refusal's detail is read once more here.
 */
export function PreviewFailure({ url, error, retry }: { url: string; error: Error; retry: () => void }) {
  const status = failedStatus(error)
  const refused = status !== null && status >= 400 && status < 500
  const [detail, setDetail] = useState<{ url: string; text: string } | null>(null)
  useEffect(() => {
    if (!refused) return
    let live = true
    fetch(url, { headers: { Accept: 'application/problem+json, application/json' } })
      .then((response) => response.json() as Promise<{ detail?: unknown }>)
      .then(
        (problem) => live && typeof problem.detail === 'string' && setDetail({ url, text: problem.detail }),
        () => undefined,
      )
    return () => {
      live = false
    }
  }, [refused, url])
  const reason = detail?.url === url ? detail.text : null
  return (
    <div role="alert" className="flex items-center gap-3 p-4 text-[13px] text-warn">
      {refused
        ? reason
          ? `The plate's preview cannot be shown: ${reason}`
          : "The plate's preview cannot be shown for this file."
        : "The plate's preview could not be loaded."}
      {!refused && (
        <Button type="button" size="sm" variant="outline" onClick={retry}>
          Try again
        </Button>
      )}
    </div>
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
        own.userData.designColor = designColorOf(mesh, own)
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

  // The copies' materials are this component's own: free them with it.
  useEffect(
    () => () =>
      scene.traverse((node) => {
        const mesh = node as THREE.Mesh
        if (!mesh.isMesh) return
        for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) material.dispose()
      }),
    [scene],
  )

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
