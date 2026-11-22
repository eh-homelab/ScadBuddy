import { createHash } from 'node:crypto'
import { CrudReadError } from '../../api/crudRead.js'
import { crudCall, type CrudTarget, segment } from '../../api/crudStores.js'
import { PluginError } from '../registry.js'
import { SHA256_RE } from './source.js'

// The blob-store copy of a bytes pin's file (#6935, spec 2026-11-21 §6.6), through the
// API's CRUD area `package-mirrors` (backend api/crud_agent_packages.py,
// agent_data/package_mirrors.py). The installer sends the file it fetched before the pin
// is stored; a load reads it back from here, never from the upstream URL, so an approved
// pin still loads after upstream deletes or changes the file.

export interface PackageMirror {
  /** Keeps `bytes` under their sha256. */
  put(sha256: string, bytes: Buffer, signal?: AbortSignal): Promise<void>
  /** The kept bytes; PluginError 502 when none are kept or they no longer hash to `sha256`. */
  get(sha256: string): Promise<Buffer>
}

/** A file of at most 256 MiB, at a LAN's speed; the CRUD default of seconds is for rows. */
const MIRROR_TIMEOUT_MS = 120_000

export class CrudPackageMirror implements PackageMirror {
  readonly #target: CrudTarget

  constructor(target: CrudTarget) {
    this.#target = target
  }

  #path(sha256: string): string {
    return `/package-mirrors/${segment(sha256, SHA256_RE, 'a sha256')}`
  }

  async put(sha256: string, bytes: Buffer, signal?: AbortSignal): Promise<void> {
    const response = await crudCall(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, "a plugin package's file mirrored", this.#path(sha256), {
      method: 'PUT',
      bytes,
      signal,
      timeoutMs: MIRROR_TIMEOUT_MS,
    })
    await response.body?.cancel()
  }

  async get(sha256: string): Promise<Buffer> {
    let bytes: Buffer
    try {
      const response = await crudCall(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, "a plugin package's mirrored file", this.#path(sha256), {
        timeoutMs: MIRROR_TIMEOUT_MS,
      })
      bytes = Buffer.from(await response.arrayBuffer())
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 404) {
        throw new PluginError(`no mirrored copy of the pinned file ${sha256} is kept`, 502)
      }
      throw err
    }
    if (createHash('sha256').update(bytes).digest('hex') !== sha256) {
      throw new PluginError(`the mirrored copy of ${sha256} does not hash to it`, 502)
    }
    return bytes
  }
}
