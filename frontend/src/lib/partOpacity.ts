import type * as THREE from 'three'
import { designOpacityOf } from './glbColor'

/**
 * Owner, 2026-11-14 — a translucent filament draws see-through. The opacity comes from
 * the GLB (`designOpacityOf`), as the covers' does (`render/thumbnail.py`).
 */
export function seeThrough(material: THREE.Material, opacity: number): void {
  material.transparent = true
  material.opacity = opacity
  // A translucent part writes no depth, so the parts inside it are still drawn.
  material.depthWrite = false
  material.needsUpdate = true
}

/**
 * The preview's translucent parts. `scene` is a `clone(true)` of the loader's scene, whose
 * materials are still the loader's cache's, shared with every other copy: a translucent
 * mesh gets clones of its materials, and no shared material changes. It draws after the
 * opaque meshes (`renderOrder` 1). Returns the clones, for the caller to dispose.
 */
export function withPartOpacity(scene: THREE.Object3D): THREE.Material[] {
  const owned: THREE.Material[] = []
  scene.traverse((node) => {
    const mesh = node as THREE.Mesh
    if (!mesh.isMesh) return
    const opacity = designOpacityOf(mesh)
    if (opacity >= 1) return
    const own = (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).map((material) => {
      const copy = material.clone()
      seeThrough(copy, opacity)
      owned.push(copy)
      return copy
    })
    mesh.material = Array.isArray(mesh.material) ? own : own[0]!
    mesh.renderOrder = 1
  })
  return owned
}
