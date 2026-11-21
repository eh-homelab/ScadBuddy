import * as THREE from 'three'
import { describe, expect, it } from 'vitest'
import { designOpacityOf } from './glbColor'
import { applyPartOpacity, recolourParts, slotOpacity, withPartOpacity } from './partOpacity'

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

  it('floors a clear part so it stays visible (#6349)', () => {
    const { root, clear } = scene(0)
    withPartOpacity(root)
    expect(designOpacityOf(clear)).toBe(0.35)
    expect((clear.material as THREE.MeshStandardMaterial).opacity).toBe(0.35)
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

describe('applyPartOpacity (#6349)', () => {
  it('turns a material see-through and back to solid, on the mesh it draws', () => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshStandardMaterial())
    const material = mesh.material as THREE.MeshStandardMaterial
    applyPartOpacity(mesh, material, 0.5)
    expect([material.transparent, material.opacity, material.depthWrite, material.side, mesh.renderOrder]).toEqual([
      true,
      0.5,
      false,
      THREE.FrontSide,
      1,
    ])
    applyPartOpacity(mesh, material, 1)
    expect([material.transparent, material.opacity, material.depthWrite, material.side, mesh.renderOrder]).toEqual([
      false,
      1,
      true,
      THREE.DoubleSide,
      0,
    ])
  })
})

describe('applyPartOpacity recompiles only on a change (#6349)', () => {
  it('leaves the material alone while it stays opaque, and recompiles when it goes see-through', () => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshStandardMaterial({ side: THREE.DoubleSide }))
    const material = mesh.material as THREE.MeshStandardMaterial
    const version = material.version
    applyPartOpacity(mesh, material, 1)
    expect(material.version).toBe(version)
    applyPartOpacity(mesh, material, 0.5)
    expect(material.version).toBe(version + 1)
    applyPartOpacity(mesh, material, 0.4)
    expect(material.version).toBe(version + 1)
    applyPartOpacity(mesh, material, 1)
    expect(material.version).toBe(version + 2)
  })
})

describe('recolourParts (#6349)', () => {
  it('draws a part see-through for a translucent spool and solid again after a change of spool', () => {
    const material = new THREE.MeshStandardMaterial()
    material.userData.designColor = '#FF0000'
    material.userData.designOpacity = 1
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(), material)
    const root = new THREE.Group()
    root.add(mesh)

    recolourParts(root, new Map([['#FF0000', '#00FF0080']]))
    expect(material.color.getHexString()).toBe('00ff00')
    expect([material.transparent, material.side, mesh.renderOrder]).toEqual([true, THREE.FrontSide, 1])

    recolourParts(root, new Map([['#FF0000', '#0000FF']]))
    expect(material.color.getHexString()).toBe('0000ff')
    expect([material.transparent, material.opacity, material.side, mesh.renderOrder]).toEqual([
      false,
      1,
      THREE.DoubleSide,
      0,
    ])
  })
})

describe('recolourParts with an opaque spool (#6349 review)', () => {
  it('draws a translucent design part solid for a spool that spells FF', () => {
    const material = new THREE.MeshStandardMaterial()
    material.userData.designColor = '#FF0000'
    material.userData.designOpacity = 0.5
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(), material)
    const root = new THREE.Group()
    root.add(mesh)

    recolourParts(root, new Map([['#FF0000', '#00FF00FF']]))
    expect(material.color.getHexString()).toBe('00ff00')
    expect([material.transparent, material.opacity, material.depthWrite, material.side, mesh.renderOrder]).toEqual([
      false,
      1,
      true,
      THREE.DoubleSide,
      0,
    ])
  })
})

describe('slotOpacity (#6349)', () => {
  it("takes the alpha of the colour a slot prints in when it has one, else the part's own", () => {
    expect(slotOpacity('#FFFFFF80', 1)).toBeCloseTo(0.502, 2)
    expect(slotOpacity('#FFFFFF00', 1)).toBe(0.35)
    expect(slotOpacity('#FFFFFF', 0.5)).toBe(0.5)
    expect(slotOpacity(undefined, 0.5)).toBe(0.5)
    expect(slotOpacity('#FFFFFFFF', 0.5)).toBe(1)
  })
})
