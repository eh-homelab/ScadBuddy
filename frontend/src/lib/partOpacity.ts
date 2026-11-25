import * as THREE from 'three'
import { normalizeHex } from './format'
import { designColorOf, designOpacityOf } from './glbColor'
import { previewOpacity } from './rgba'

const EXPLICIT_ALPHA = /^#?[0-9A-Fa-f]{8}$/

/**
 * #6349 — the opacity of a part printed in `colour`, the colour its slot's spool gives it.
 * A colour that spells an alpha (`#RRGGBBAA`, `FF` included) sets it; one that does
 * not (`#RRGGBB`) leaves the part's own, from the GLB. So an opaque spool (`FF`) prints a
 * translucent design part solid.
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
  // A shader recompiles only when the material's transparency or side changes, not its opacity.
  const before = [material.transparent, material.side]
  if (opacity < 1) {
    material.side = THREE.FrontSide
    material.transparent = true
    material.opacity = opacity
    material.depthWrite = false
    mesh.renderOrder = 1
  } else {
    material.side = THREE.DoubleSide
    material.transparent = false
    material.opacity = 1
    material.depthWrite = true
    mesh.renderOrder = 0
  }
  if (material.transparent !== before[0] || material.side !== before[1]) material.needsUpdate = true
}

/**
 * #6349 — the print dialog's recolour: each part's material takes the colour its slot's
 * spool gives it (`colors`, by design colour) and its opacity (`slotOpacity`). A colour
 * with no entry is the design's own.
 */
export function recolourParts(scene: THREE.Object3D, colors: ReadonlyMap<string, string>): void {
  scene.traverse((node) => {
    const mesh = node as THREE.Mesh
    if (!mesh.isMesh) return
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      const own = material as THREE.MeshStandardMaterial
      const printed = colors.get(own.userData.designColor as string)
      own.color.set(normalizeHex(printed ?? (own.userData.designColor as string)))
      applyPartOpacity(mesh, own, slotOpacity(printed, own.userData.designOpacity as number))
    }
  })
}

/** A mesh's filament slot, from 0: the GLB's `scadbuddy_slot` extra (`render/glb.py` `SLOT_EXTRA`), or null. */
function slotOf(mesh: THREE.Object3D): number | null {
  const slot: unknown = mesh.userData.scadbuddy_slot
  return typeof slot === 'number' && Number.isInteger(slot) && slot >= 0 ? slot : null
}

/**
 * #6710 §8.2 — gives each mesh that names a slot materials of its own to recolour, each
 * holding its design colour and opacity, as `recolourParts` reads them. `scene` is a
 * `clone(true)` of the loader's scene, whose materials are shared: no shared material
 * changes. A material in `already` (`withPartOpacity`'s clones) is the mesh's own, so it is
 * kept. A mesh with no slot keeps its materials. Returns the new clones, for the caller to
 * dispose.
 */
export function ownSlotMaterials(scene: THREE.Object3D, already: readonly THREE.Material[] = []): THREE.Material[] {
  const owned: THREE.Material[] = []
  const kept = new Set(already)
  scene.traverse((node) => {
    const mesh = node as THREE.Mesh
    if (!mesh.isMesh || slotOf(mesh) === null) return
    const opacity = designOpacityOf(mesh)
    const own = (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).map((material) => {
      const copy = kept.has(material) ? material : material.clone()
      copy.userData.designColor = designColorOf(mesh, copy)
      copy.userData.designOpacity = opacity
      if (copy !== material) owned.push(copy)
      return copy
    })
    mesh.material = Array.isArray(mesh.material) ? own : own[0]!
  })
  return owned
}

/**
 * #6710 §8.2 — a file listing's recolour: each mesh that names a slot takes the colour of
 * the filament picked for that slot (`colours`, by slot from 0, `#RRGGBBAA`) and its
 * opacity (`slotOpacity`); a slot with no colour shows the file's own. A mesh with no slot
 * is left as it is. Call `ownSlotMaterials` on the scene first.
 */
export function recolourSlots(scene: THREE.Object3D, colours: Readonly<Record<number, string>>): void {
  scene.traverse((node) => {
    const mesh = node as THREE.Mesh
    const slot = mesh.isMesh ? slotOf(mesh) : null
    if (slot === null) return
    const printed = colours[slot]
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      const own = material as THREE.MeshStandardMaterial
      if (typeof own.userData.designColor !== 'string') continue
      own.color.set(normalizeHex(printed ?? (own.userData.designColor as string)))
      applyPartOpacity(mesh, own, slotOpacity(printed, own.userData.designOpacity as number))
    }
  })
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
