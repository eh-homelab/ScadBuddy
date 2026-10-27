import { createHash } from 'node:crypto'

// Where a workspace file's sealed bytes are kept (#4789): a blob of the backend's content
// store (`store_backend` s3 or bambuddy), named `workspace-<sha256 of the sealed bytes>`.
// The agent holds no store credential and mints no token: the backend's hidden routes
// (backend api/internal_workspace.py) hand it a TransferUrl to PUT the bytes to (its
// internal blob route) and one to GET them back (S3's presigned URL on s3). A URL and its
// headers are bearer credentials: never logged, never put in an error.
//
// On `store_backend` local the backend has no content store and its routes answer 404:
// `put` then answers undefined, and SessionFiles keeps the bytes in `ai_session_files.data`
// as before (an older backend without the routes answers the same).

export const WORKSPACE_BLOBS = '/api/v1/internal/workspace-blobs'

export interface WorkspaceBlobStore {
  /** The key `sealed` is stored under, or undefined when the backend keeps no blobs. */
  put(sealed: Buffer): Promise<string | undefined>
  /** The stored bytes of `key`, as they arrive. */
  get(key: string): AsyncIterable<Uint8Array>
}

type TransferUrl = { url: string; headers?: Record<string, string> }

/** A refusal of the blob routes; its message names the step and the status, never a URL. */
export class WorkspaceBlobError extends Error {
  override name = 'WorkspaceBlobError'
}

export class BackendWorkspaceBlobs implements WorkspaceBlobStore {
  readonly #base: string
  readonly #fetch: typeof fetch
  readonly #warn: (message: string) => void
  #warned = false

  constructor(baseUrl: string, options: { fetch?: typeof fetch; warn?: (message: string) => void } = {}) {
    this.#base = baseUrl.replace(/\/+$/, '')
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#warn = options.warn ?? (() => {})
  }

  async #json<T>(step: string, response: Response): Promise<T> {
    if (!response.ok) throw new WorkspaceBlobError(`workspace blob ${step} failed: HTTP ${response.status}`)
    return (await response.json()) as T
  }

  async put(sealed: Buffer): Promise<string | undefined> {
    const minted = await this.#fetch(`${this.#base}${WORKSPACE_BLOBS}/uploads`, { method: 'POST' })
    if (minted.status === 404) {
      if (!this.#warned) {
        this.#warned = true
        const detail = await (minted.json() as Promise<{ detail?: unknown }>)
          .then((body) => (typeof body.detail === 'string' ? body.detail : ''))
          .catch(() => '')
        this.#warn(`workspace files stay in Postgres: the backend keeps no blobs for them${detail ? ` (${detail})` : ''}`)
      } else {
        await minted.body?.cancel()
      }
      return undefined
    }
    const upload = await this.#json<{ url: TransferUrl; pending_key: string }>('upload', minted)
    const put = await this.#fetch(upload.url.url, {
      method: 'PUT',
      headers: { ...upload.url.headers, 'content-type': 'application/octet-stream' },
      body: sealed,
      redirect: 'error',
    })
    await put.body?.cancel()
    if (!put.ok) throw new WorkspaceBlobError(`workspace blob PUT failed: HTTP ${put.status}`)
    const finished = await this.#fetch(`${this.#base}${WORKSPACE_BLOBS}/uploads/${encodeURIComponent(upload.pending_key)}/finish`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sha256: createHash('sha256').update(sealed).digest('hex'), size: sealed.length }),
    })
    return (await this.#json<{ key: string }>('finish', finished)).key
  }

  async *get(key: string): AsyncIterable<Uint8Array> {
    const minted = await this.#fetch(`${this.#base}${WORKSPACE_BLOBS}/${encodeURIComponent(key)}/url`)
    const url = await this.#json<TransferUrl>(`URL of ${key}`, minted)
    const response = await this.#fetch(url.url, { headers: url.headers ?? {}, redirect: 'error' })
    if (!response.ok || !response.body) {
      await response.body?.cancel()
      throw new WorkspaceBlobError(`workspace blob ${key} could not be read: HTTP ${response.status}`)
    }
    yield* response.body
  }
}
