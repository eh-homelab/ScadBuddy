import * as THREE from 'three'
import { normalizeHex } from './format'

/**
 * A part's design colour, as the slots name it. The backend writes the exact sRGB hex as
 * the mesh's `scadbuddy_colour` extra because the material's factor is stored in 8 bits
 * and cannot be read back exactly (#1319: #808080 comes back #7F7F7F). A preview from
 * before #1319 has no extra and wrote the sRGB bytes as the factor, so that is read raw,
 * as `render/glb.py`'s `_colour_of` does.
 */
export function designColorOf(mesh: THREE.Object3D, material: THREE.Material): string {
  const exact: unknown = mesh.userData.scadbuddy_colour
  if (typeof exact === 'string') return normalizeHex(exact)
  const color = (material as THREE.MeshStandardMaterial).color
  return normalizeHex(`#${color.getHexString(THREE.LinearSRGBColorSpace)}`)
}
