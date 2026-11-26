import { createHash } from 'node:crypto'
import type { PackageMirror } from '../../src/plugins/packages/mirror.js'
import { PluginError } from '../../src/plugins/registry.js'

/**
 * The API's area `package-mirrors` in memory: a file is read back only by the package it was
 * sent for. `failPut` makes a send fail, `down` every read; `gets` counts the reads.
 */
export function memoryMirror(): PackageMirror & { kept: Map<string, Buffer>; failPut: boolean; down: boolean; gets: number } {
  const kept = new Map<string, Buffer>()
  const names = new Map<string, string>()
  const mirror = {
    kept,
    failPut: false,
    down: false,
    gets: 0,
    put: async (name: string, sha: string, bytes: Buffer) => {
      if (mirror.failPut) throw new PluginError('the API did not answer', 502)
      if (createHash('sha256').update(bytes).digest('hex') !== sha) throw new Error(`bytes sent as ${sha} do not hash to it`)
      kept.set(sha, Buffer.from(bytes))
      names.set(sha, name)
    },
    get: async (name: string, sha: string) => {
      mirror.gets += 1
      if (mirror.down) throw new PluginError('the API did not answer', 502)
      const bytes = kept.get(sha)
      if (!bytes || names.get(sha) !== name) throw new PluginError(`no mirrored copy of the pinned file ${sha} is kept`, 502)
      return bytes
    },
  }
  return mirror
}
