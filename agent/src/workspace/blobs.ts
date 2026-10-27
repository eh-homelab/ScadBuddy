import { createHash } from 'node:crypto'

// Where a workspace file's sealed bytes are kept (#4789): a blob of the backend's content
// store (`store_backend` s3 or bambuddy), named `workspace-<sha256 of the sealed bytes>`.
// The agent holds no store credential and mints no token: the backend's hidden routes
// (backend api/internal_workspace.py) hand it a TransferUrl to PUT the bytes to (its
// internal blob route) and one to GET them back (S3's presigned URL on s3). A URL and its
// headers are bearer credentials: never logged, never put in an error.
//
// Which store the backend runs on is what its /healthz says (`store.backend`). Only on
// `local`, which has no content store, do the bytes stay in `ai_session_files.data`. On any
// other backend a write that cannot reach the blob store is refused (WorkspaceBlobError):
// a backend without SCADBUDDY_API_INTERNAL_URL or the KEK (the routes answer 503), one
// older than the routes (404), or one that cannot be reached. Nothing is then kept in
// plaintext.

export const WORKSPACE_BLOBS = '/api/v1/internal/workspace-blobs'

/** How long the backend's `store_backend` is kept: it changes only when the backend restarts. */
export const BACKEND_CACHE_MS = 60_000

export interface WorkspaceBlobStore {
  /** The backend's `store_backend` (`local`, `s3`, `bambuddy`); WorkspaceBlobError when it cannot be read. */
  backend(): Promise<string>
  /** Whether the backend mints the URLs a write needs; WorkspaceBlobError saying why not. */
  probe(): Promise<void>
  /** The key `sealed` is stored under; WorkspaceBlobError when it cannot be stored. */
  put(sealed: Buffer): Promise<string>
  /** The stored bytes of `key`, as they arrive. */
  get(key: string): AsyncIterable<Uint8Array>
}

type TransferUrl = { url: string; headers?: Record<string, string> }

/** A refusal of the blob routes; its message names the step and why, never a URL. */
export class WorkspaceBlobError extends Error {
  override name = 'WorkspaceBlobError'
}

/** The `detail` of a problem response, or ''. */
async function detailOf(response: Response): Promise<string> {
  const body = (await response.json().catch(() => undefined)) as { detail?: unknown } | undefined
  return typeof body?.detail === 'string' ? body.detail : ''
}

export class BackendWorkspaceBlobs implements WorkspaceBlobStore {
  readonly #base: string
  readonly #fetch: typeof fetch
  readonly #cacheMs: number
  #backend: { name: string; until: number } | undefined

  constructor(baseUrl: string, options: { fetch?: typeof fetch; backendCacheMs?: number } = {}) {
    this.#base = baseUrl.replace(/\/+$/, '')
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#cacheMs = options.backendCacheMs ?? BACKEND_CACHE_MS
  }

  async #call(step: string, url: string, init?: RequestInit): Promise<Response> {
    try {
      return await this.#fetch(url, init)
    } catch {
      throw new WorkspaceBlobError(`the backend could not be reached (${step})`)
    }
  }

  async #json<T>(step: string, response: Response): Promise<T> {
    if (response.ok) return (await response.json()) as T
    if (response.status === 404 && step === 'upload') {
      await response.body?.cancel()
      throw new WorkspaceBlobError('the backend has no workspace blob routes: it is older than #4789; upgrade it')
    }
    const detail = await detailOf(response)
    throw new WorkspaceBlobError(`the backend refused the workspace blob ${step} (HTTP ${response.status}${detail ? `: ${detail}` : ''})`)
  }

  async backend(): Promise<string> {
    if (this.#backend && this.#backend.until > Date.now()) return this.#backend.name
    const response = await this.#call('/healthz', `${this.#base}/healthz`)
    const health = response.ok ? ((await response.json().catch(() => undefined)) as { store?: { backend?: unknown } } | undefined) : undefined
    if (!response.ok) await response.body?.cancel()
    const name = health?.store?.backend
    if (typeof name !== 'string') throw new WorkspaceBlobError(`the backend's /healthz names no store backend (HTTP ${response.status})`)
    this.#backend = { name, until: Date.now() + this.#cacheMs }
    return name
  }

  async probe(): Promise<void> {
    const minted = await this.#call('upload', `${this.#base}${WORKSPACE_BLOBS}/uploads`, { method: 'POST' })
    await this.#json<unknown>('upload', minted)
  }

  async put(sealed: Buffer): Promise<string> {
    const minted = await this.#call('upload', `${this.#base}${WORKSPACE_BLOBS}/uploads`, { method: 'POST' })
    const upload = await this.#json<{ url: TransferUrl; pending_key: string }>('upload', minted)
    const put = await this.#call('PUT', upload.url.url, {
      method: 'PUT',
      headers: { ...upload.url.headers, 'content-type': 'application/octet-stream' },
      body: sealed,
      redirect: 'error',
    })
    await put.body?.cancel()
    if (!put.ok) throw new WorkspaceBlobError(`the backend refused the workspace blob PUT (HTTP ${put.status})`)
    const finished = await this.#call('finish', `${this.#base}${WORKSPACE_BLOBS}/uploads/${encodeURIComponent(upload.pending_key)}/finish`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sha256: createHash('sha256').update(sealed).digest('hex'), size: sealed.length }),
    })
    return (await this.#json<{ key: string }>('finish', finished)).key
  }

  async *get(key: string): AsyncIterable<Uint8Array> {
    const minted = await this.#call('URL', `${this.#base}${WORKSPACE_BLOBS}/${encodeURIComponent(key)}/url`)
    const url = await this.#json<TransferUrl>(`URL of ${key}`, minted)
    const response = await this.#call('GET', url.url, { headers: url.headers ?? {}, redirect: 'error' })
    if (!response.ok || !response.body) {
      await response.body?.cancel()
      throw new WorkspaceBlobError(`workspace blob ${key} could not be read (HTTP ${response.status})`)
    }
    yield* response.body
  }
}
