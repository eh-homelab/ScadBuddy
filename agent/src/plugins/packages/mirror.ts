import { createHash } from 'node:crypto'
import { CrudReadError } from '../../api/crudRead.js'
import { crudCall, type CrudTarget, segment } from '../../api/crudStores.js'
import { PluginError } from '../registry.js'
import { SHA256_RE } from './source.js'
import { PACKAGE_NAME_RE } from './store.js'

// The blob-store copy of a bytes pin's file (#6935, spec 2026-11-21 §6.6), through the
// API's CRUD area `package-mirrors` (backend api/crud_agent_packages.py,
// agent_data/package_mirrors.py). The installer sends the file it fetched for a package
// (`POST …/package-mirrors/{name}`, answered with the sha256 the API kept it under)
// before the pin is stored; a load reads it back (`GET …/package-mirrors/{name}/{sha256}`,
// served only while that package's pin or pending re-pin names the sha256) from here,
// never from the upstream URL, so an approved pin still loads after upstream deletes or
// changes the file.
//
// A commit pin's files go here too (#7907), as a zip (zip.ts `storedZip`), and the
// package's row records its sha256 (`mirror_sha256`, store.ts `recordMirror`): a pod with a
// cold cache unpacks that zip and does not fetch the repository again (install.ts).

export interface PackageMirror {
  /** Keeps `bytes` (whose sha256 is `sha256`) for the package `name`. */
  put(name: string, sha256: string, bytes: Buffer, signal?: AbortSignal): Promise<void>
  /** The kept bytes of `name`'s pin; PluginError 502 when none are kept for it or they no longer hash to `sha256`. */
  get(name: string, sha256: string): Promise<Buffer>
}

/**
 * The largest mirror the API keeps (backend agent_data/package_mirrors.py `MAX_MIRROR_BYTES`;
 * keep the two equal): the larger of a downloaded file (`ARCHIVE_MAX_BYTES`, download.ts,
 * 21 MiB) and a commit pin's stored zip (#7907). That zip holds at most the content hash's
 * 20 MiB of files (hash.ts) plus 76 bytes and twice the path for each of at most 2,000
 * entries (zip.ts, no compression and no directory entries), so 4 MiB of room fits paths of
 * 1,010 bytes on average. A zip past it is never sent.
 */
export const MIRROR_MAX_BYTES = 24 * 1024 * 1024

/** A file of at most `MIRROR_MAX_BYTES`, at a LAN's speed; the CRUD default of seconds is for rows. */
const MIRROR_TIMEOUT_MS = 120_000

export class CrudPackageMirror implements PackageMirror {
  readonly #target: CrudTarget

  constructor(target: CrudTarget) {
    this.#target = target
  }

  async put(name: string, sha256: string, bytes: Buffer, signal?: AbortSignal): Promise<void> {
    const path = `/package-mirrors/${segment(name, PACKAGE_NAME_RE, 'a package name')}`
    const response = await crudCall(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, "a plugin package's file mirrored", path, {
      method: 'POST',
      bytes,
      signal,
      timeoutMs: MIRROR_TIMEOUT_MS,
    })
    const kept = ((await response.json()) as { bytes_sha256?: unknown }).bytes_sha256
    if (kept !== sha256) throw new PluginError(`the API kept the bytes as ${String(kept)}, not ${sha256}`, 502)
  }

  async get(name: string, sha256: string): Promise<Buffer> {
    const path = `/package-mirrors/${segment(name, PACKAGE_NAME_RE, 'a package name')}/${segment(sha256, SHA256_RE, 'a sha256')}`
    let bytes: Buffer
    try {
      const response = await crudCall(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, "a plugin package's mirrored file", path, {
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
