import { createHash } from 'node:crypto'
import { CrudReadError } from '../../api/crudRead.js'
import { crudCall, type CrudTarget, crudOf, segment } from '../../api/crudStores.js'
import { PluginError } from '../registry.js'
import { PACKAGE_NAME_RE } from './store.js'

// A hosted plugin's published versions, as the installer reads them (#6934, spec 2026-11-21
// §6.3 step 5 and §6.5), over the API's CRUD area `hosted-plugins` (backend
// api/crud_agent_packages.py): `GET …/hosted-plugins/{name}` the plugin and its latest
// version's row, `GET …/hosted-plugins/{name}/versions/{N}.zip` that version's zip, as
// published. A version is never changed, so its zip is the pin's bytes: the installer
// checks the zip against the row's `zip_sha256` and the unpacked files against its
// `content_hash`, then vets them, at install and at every cache miss.

/** A published version: the pin is (`version`, `content_hash`). */
export type HostedVersion = { version: number; zip_sha256: string; content_hash: string }

export interface HostedPlugins {
  /** The plugin's latest published version; undefined when there is no such plugin, null before its first publish. */
  latest(name: string): Promise<HostedVersion | null | undefined>
  /** The zip of `version`, checked against `zip_sha256` when given; PluginError 502 when it is gone or differs. */
  zip(name: string, version: number, zipSha256?: string): Promise<Buffer>
}

/** A version's zip: as large as a hosted plugin's (20 MiB unpacked), at a LAN's speed. */
const ZIP_TIMEOUT_MS = 120_000

export class CrudHostedPlugins implements HostedPlugins {
  readonly #target: CrudTarget
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#target = target
    this.#call = crudOf(target)
  }

  async latest(name: string): Promise<HostedVersion | null | undefined> {
    if (!PACKAGE_NAME_RE.test(name)) return undefined
    const { plugin } = await this.#call<{ plugin: { latest: HostedVersion | null } | null }>(
      'a hosted plugin',
      `/hosted-plugins/${segment(name, PACKAGE_NAME_RE, 'a package name')}`,
    )
    if (!plugin) return undefined
    return plugin.latest ? { version: plugin.latest.version, zip_sha256: plugin.latest.zip_sha256, content_hash: plugin.latest.content_hash } : null
  }

  async zip(name: string, version: number, zipSha256?: string): Promise<Buffer> {
    if (!Number.isSafeInteger(version) || version < 1) throw new PluginError(`${version} is not a hosted plugin version`, 400)
    const path = `/hosted-plugins/${segment(name, PACKAGE_NAME_RE, 'a package name')}/versions/${version}.zip`
    let bytes: Buffer
    try {
      const response = await crudCall(this.#target.fetch ?? globalThis.fetch, this.#target.baseUrl, 'a hosted plugin version', path, {
        timeoutMs: ZIP_TIMEOUT_MS,
      })
      bytes = Buffer.from(await response.arrayBuffer())
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 404) {
        throw new PluginError(`the hosted plugin ${name} has no version ${version}`, 502)
      }
      throw err
    }
    if (zipSha256 !== undefined && createHash('sha256').update(bytes).digest('hex') !== zipSha256) {
      throw new PluginError(`the zip of ${name} version ${version} does not hash to ${zipSha256}`, 502)
    }
    return bytes
  }
}
