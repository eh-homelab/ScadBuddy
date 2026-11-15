import * as THREE from 'three'
import { describe, expect, it } from 'vitest'
import { designOpacityOf } from './glbColor'
import { withPartOpacity } from './partOpacity'

/** Two meshes sharing one material, as the loader's cache gives them. */
function scene(alpha?: number) {
  const shared = new THREE.MeshStandardMaterial({ color: '#ffffff' })
  const clear = new THREE.Mesh(new THREE.BoxGeometry(), shared)
  clear.userData.scadbuddy_colour = '#FFFFFF'
  if (alpha !== undefined) clear.userData.scadbuddy_alpha = alpha
  const core = new THREE.Mesh(new THREE.BoxGeometry(), shared)
  core.userData.scadbuddy_colour = '#FF0000'
  const root = new THREE.Group()
  root.add(clear, core)
  return { root, shared, clear, core }
}

describe("a part's opacity (owner, 2026-11-14)", () => {
  it("reads the GLB's scadbuddy_alpha extra, opaque without one", () => {
    const { clear, core } = scene(128)
    expect(designOpacityOf(clear)).toBeCloseTo(128 / 255)
    expect(designOpacityOf(core)).toBe(1)
  })

  it('makes only the translucent part see-through, on a clone, after the opaque ones', () => {
    const { root, shared, clear, core } = scene(128)
    const owned = withPartOpacity(root)

    expect(owned).toHaveLength(1)
    const own = clear.material as THREE.MeshStandardMaterial
    expect(own).not.toBe(shared)
    expect(own.transparent).toBe(true)
    expect(own.opacity).toBeCloseTo(128 / 255)
    expect(own.depthWrite).toBe(false)
    expect(clear.renderOrder).toBe(1)

    // The opaque part keeps the shared material, and the shared one is untouched.
    expect(core.material).toBe(shared)
    expect(core.renderOrder).toBe(0)
    expect(shared.transparent).toBe(false)
    expect(shared.opacity).toBe(1)
    expect(shared.depthWrite).toBe(true)
  })

  it('changes nothing in a scene with no translucent part', () => {
    const { root, shared, clear, core } = scene()
    expect(withPartOpacity(root)).toEqual([])
    expect(clear.material).toBe(shared)
    expect(core.material).toBe(shared)
    expect(clear.renderOrder).toBe(0)
  })
})
