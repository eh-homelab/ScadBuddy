import { readFileSync } from 'node:fs'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { describe, expect, it } from 'vitest'
import { designColorOf } from './glbColor'

/** Each mesh of a parsed GLB with its design colour. */
async function designColors(bytes: Uint8Array): Promise<string[]> {
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer
  const gltf = await new GLTFLoader().parseAsync(buffer, '')
  const out: string[] = []
  gltf.scene.traverse((node) => {
    const mesh = node as THREE.Mesh
    if (mesh.isMesh) out.push(designColorOf(mesh, mesh.material as THREE.Material))
  })
  return out
}

describe('designColorOf (#1723 review)', () => {
  it("reads a dark and a mid-grey part's exact colour from a GLB the backend wrote", async () => {
    // `render/glb.py` `write_glb` output: #808080 and #202020, whose 8-bit factors read
    // back as #7F7F7F and about #222222.
    const glb = readFileSync(`${import.meta.dirname}/../test/fixtures/grey-parts.glb`)
    expect(await designColors(new Uint8Array(glb))).toEqual(['#808080', '#202020'])
  })

  it('reads a pre-#1319 preview, which wrote the sRGB bytes as the factor, raw', () => {
    const material = new THREE.MeshStandardMaterial()
    material.color.setRGB(0x80 / 255, 0x20 / 255, 0xff / 255, THREE.LinearSRGBColorSpace)
    expect(designColorOf(new THREE.Mesh(undefined, material), material)).toBe('#8020FF')
  })
})
