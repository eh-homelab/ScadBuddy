import * as THREE from 'three'
import { describe, expect, it } from 'vitest'
import { designOpacityOf } from './glbColor'
import { applyPartOpacity, ownSlotMaterials, recolourParts, recolourSlots, slotOpacity, withPartOpacity } from './partOpacity'

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

describe('recolourSlots (#6710 §8.2)', () => {
  function slotted() {
    const shared = new THREE.MeshStandardMaterial({ color: '#808080' })
    const body = new THREE.Mesh(new THREE.BoxGeometry(), shared)
    body.userData.scadbuddy_colour = '#FF6AC1'
    body.userData.scadbuddy_slot = 0
    const top = new THREE.Mesh(new THREE.BoxGeometry(), shared)
    top.userData.scadbuddy_colour = '#1F6FEB'
    top.userData.scadbuddy_slot = 1
    const loose = new THREE.Mesh(new THREE.BoxGeometry(), shared)
    loose.userData.scadbuddy_colour = '#00FF00'
    const root = new THREE.Group()
    root.add(body, top, loose)
    return { root, shared, body, top, loose }
  }

  it("colours each mesh by its slot's colour, on materials of its own", () => {
    const { root, shared, body, top, loose } = slotted()
    const owned = ownSlotMaterials(root)
    expect(owned).toHaveLength(2)
    recolourSlots(root, { 0: '#E8532FFF', 1: '#1B6CA880' })
    const bodyMaterial = body.material as THREE.MeshStandardMaterial
    const topMaterial = top.material as THREE.MeshStandardMaterial
    expect(bodyMaterial).not.toBe(shared)
    expect(bodyMaterial.color.getHexString(THREE.SRGBColorSpace)).toBe('e8532f')
    expect(bodyMaterial.transparent).toBe(false)
    expect(topMaterial.color.getHexString(THREE.SRGBColorSpace)).toBe('1b6ca8')
    expect(topMaterial.transparent).toBe(true)
    // A mesh with no slot is left as it is, and the loader's shared material is untouched.
    expect(loose.material).toBe(shared)
    expect(shared.color.getHexString(THREE.SRGBColorSpace)).toBe('808080')
  })

  it("keeps a slot with no colour given in the file's own colour", () => {
    const { root, body } = slotted()
    ownSlotMaterials(root)
    recolourSlots(root, { 1: '#000000FF' })
    expect((body.material as THREE.MeshStandardMaterial).color.getHexString(THREE.SRGBColorSpace)).toBe('ff6ac1')
  })
})
