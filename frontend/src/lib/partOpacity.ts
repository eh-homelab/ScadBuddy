import * as THREE from 'three'
import { designOpacityOf } from './glbColor'
import { previewOpacity } from './rgba'

const EXPLICIT_ALPHA = /^#?[0-9A-Fa-f]{8}$/

/**
 * #6349 — the opacity of a part printed in `colour`, the colour its slot's spool gives it.
 * A colour that spells an alpha (`#RRGGBBAA`) sets it; one that does not (the dialog's
 * spool colours are `#RRGGBB` today) leaves the part's own, from the GLB.
 */
export function slotOpacity(colour: string | undefined, designOpacity: number): number {
  return colour !== undefined && EXPLICIT_ALPHA.test(colour) ? previewOpacity(colour) : designOpacity
}

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
 * #6349 — a part's material see-through at `opacity` (below 1), or solid again at 1. While
 * see-through it draws front faces only, so back faces do not blend in as a second layer,
 * and after the opaque meshes.
 */
export function applyPartOpacity(mesh: THREE.Mesh, material: THREE.Material, opacity: number): void {
  if (opacity < 1) {
    material.side = THREE.FrontSide
    seeThrough(material, opacity)
    mesh.renderOrder = 1
    return
  }
  material.side = THREE.DoubleSide
  material.transparent = false
  material.opacity = 1
  material.depthWrite = true
  material.needsUpdate = true
  mesh.renderOrder = 0
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
