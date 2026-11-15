import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { CrudReadError } from '../src/api/crudRead.js'
import { WORKSPACE_AREAS } from '../src/api/crudAreas.js'
import { event, type ServerEvent } from '../src/sessions/protocol.js'
import { CrudSessionFiles, gatedWorkspaceFiles } from '../src/workspace/crudStore.js'
import { type SessionFiles, WorkspaceError, type WorkspaceRef } from '../src/workspace/store.js'

// The workspace files through the API's CRUD routes (#5299 step 7, area `workspace`), against a
// fake API that keeps them in memory as the backend's api/crud_agent_workspace.py does (the
// backend's tests/crud/test_crud_agent_workspace.py covers the routes, the sealing and the SQL).

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b6d8a52-3a4e-4d36-9a3c-2f8c1d1e0f11'
const ws: WorkspaceRef = { kind: 'session', sessionId: SESSION }
const sha = (b: Buffer | string) => createHash('sha256').update(b).digest('hex')

type Stored = { mediaType: string; bytes: Buffer; source: unknown }

class FakeApi {
  readonly files = new Map<string, Stored>()
  readonly seen: { method: string; path: string; query: URLSearchParams; body?: unknown }[] = []
  seq = 0
  /** Answer every request as a proxy would: no CRUD header. */
  proxy = false
  /** Cut the next byte stream short. */
  cut = false
  movesLeft = 2

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = url.pathname.slice(PREFIX.length)
    let body: unknown
    if (request.method === 'POST' && request.headers.get('content-type')?.startsWith('application/json')) body = await request.clone().json()
    this.seen.push({ method: request.method, path, query: url.searchParams, body })
    if (this.proxy) return new Response('bad gateway', { status: 502 })
    const answer = await this.#route(request, path, url.searchParams, body)
    if (answer instanceof Response) return answer
    return new Response(JSON.stringify(answer.body), { status: answer.status ?? 200, headers: { 'x-scadbuddy-crud': '1' } })
  }

  #info(key: string, stored: Stored) {
    const path = key.slice(key.indexOf('|') + 1)
    const at = new Date(0).toISOString()
    return { path, media_type: stored.mediaType, size: stored.bytes.length, sha256: sha(stored.bytes), source: stored.source, created_at: at, updated_at: at }
  }

  async #route(request: Request, path: string, q: URLSearchParams, rawBody: unknown): Promise<Response | { status?: number; body: unknown }> {
    const key = (w: string | null, p: string | null) => `${w}|${p}`
    const body = rawBody as { workspace: string; path: string; events: string[] }
    if (path === '/workspace/status') return { body: { store: 'blobs', backend: 's3', sealed_unreadable: null } }
    if (path === '/workspace/file') {
      const stored = this.files.get(key(q.get('workspace'), q.get('path')))
      return { body: { file: stored ? this.#info(key(q.get('workspace'), q.get('path')), stored) : null } }
    }
    if (path === '/workspace/files') {
      const files = [...this.files].filter(([k]) => k.startsWith(`${q.get('workspace')}|`)).map(([k, v]) => this.#info(k, v))
      return { body: { files } }
    }
    if (path === '/workspace/usage') return { body: { files: this.files.size, bytes: 3 } }
    if (path === '/workspace/file-bytes') {
      const stored = this.files.get(key(q.get('workspace'), q.get('path')))
      if (!stored) return { status: 404, body: { detail: `no file ${q.get('path')} in the workspace`, reason: 'not_found' } }
      if (sha(stored.bytes) !== q.get('expected_sha256')) return { status: 409, body: { detail: 'it changed', reason: 'conflict' } }
      const cut = this.cut
      this.cut = false
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new Uint8Array(stored.bytes.subarray(0, 1)))
          if (cut) controller.error(new Error('terminated'))
          else {
            controller.enqueue(new Uint8Array(stored.bytes.subarray(1)))
            controller.close()
          }
        },
      })
      return new Response(stream, { headers: { 'x-scadbuddy-crud': '1', 'x-scadbuddy-sha256': sha(stored.bytes) } })
    }
    if (path === '/workspace/file-writes') {
      const form = await request.formData()
      const meta = JSON.parse(String(form.get('meta'))) as { workspace: string; path: string; media_type: string; expect: string; source: unknown; events: string[] }
      const bytes = Buffer.from(await (form.get('file') as File).arrayBuffer())
      this.seen.at(-1)!.body = meta
      const k = key(meta.workspace, meta.path)
      if (meta.expect === 'absent' && this.files.has(k)) return { status: 409, body: { detail: `${meta.path} already exists`, reason: 'conflict' } }
      this.files.set(k, { mediaType: meta.media_type, bytes, source: meta.source })
      const seqs = meta.events.map(() => ++this.seq)
      return { body: { file: this.#info(k, this.files.get(k)!), seqs } }
    }
    if (path === '/workspace/file-deletes') {
      const deleted = this.files.delete(key(body.workspace, body.path))
      return { body: { deleted, seqs: deleted ? body.events.map(() => ++this.seq) : [] } }
    }
    if (path === '/workspace/blob-moves') {
      const more = this.movesLeft > 0
      this.movesLeft -= 1
      return { body: { moved: more ? 2 : 0, local: false, more } }
    }
    if (path === '/workspace/reconciles') return { body: { released: 0, held: 0, skipped: 'held' } }
    if (path === '/workspace/sweeps') return { body: { swept: 1 } }
    if (path === '/workspace/copies') return { body: { copied: 1 } }
    if (path === '/workspace/session-files') return { body: { sessions: {} } }
    return { status: 404, body: { detail: 'no route' } }
  }
}

function setup() {
  const api = new FakeApi()
  return { api, files: new CrudSessionFiles(BASE, { fetch: api.fetch, limits: { fileMaxBytes: 1024, workspaceMaxBytes: 4096 } }) }
}

const uploaded = (path: string): ServerEvent =>
  event({ type: 'workspace.file', sessionId: SESSION, path, change: 'uploaded', by: { kind: 'browser', id: 'browser', label: 'You' } })

describe('CrudSessionFiles (#5299 step 7)', () => {
  it('names the area the agent waits for', () => {
    expect(WORKSPACE_AREAS).toEqual(['workspace'])
  })

  it('writes the bytes as multipart, logs the events in the write and hears of them after', async () => {
    const { api, files } = setup()
    const committed: number[][] = []
    const info = await files.write(ws, './notes/a.txt', Buffer.from('hello'), {
      expectedSha256: null,
      source: { kind: 'upload' },
      log: { events: (file) => [uploaded(file.path)], committed: (_events, seqs) => committed.push([...seqs]) },
    })
    expect(info.path).toBe('notes/a.txt')
    const meta = api.seen.at(-1)!.body as Record<string, unknown>
    expect(meta).toMatchObject({
      workspace: `session:${SESSION}`,
      path: 'notes/a.txt',
      media_type: 'text/plain',
      sha256: sha('hello'),
      size: 5,
      expect: 'absent',
      expected_sha256: null,
      source: { kind: 'upload' },
    })
    expect((meta.events as string[]).map((e) => JSON.parse(e).path)).toEqual(['notes/a.txt'])
    expect(committed).toEqual([[1]])
    // The bytes went as bytes: the meta part carries no copy of them.
    expect(JSON.stringify(meta)).not.toContain(Buffer.from('hello').toString('base64'))
  })

  it('reads a file back, and a refusal is the WorkspaceError it names', async () => {
    const { files } = setup()
    await files.write(ws, 'a.bin', Buffer.from([1, 2, 3]))
    expect((await files.read(ws, 'a.bin'))?.bytes).toEqual(Buffer.from([1, 2, 3]))
    expect(await files.read(ws, 'missing')).toBeUndefined()
    const conflict = files.write(ws, 'a.bin', Buffer.from('x'), { expectedSha256: null })
    await expect(conflict).rejects.toMatchObject({ name: 'WorkspaceError', code: 'conflict' })
    await expect(files.chunks(ws, 'a.bin', '0'.repeat(64)).next()).rejects.toMatchObject({ code: 'conflict' })
    await expect(files.chunks(ws, 'gone', '0'.repeat(64)).next()).rejects.toMatchObject({ code: 'not_found' })
  })

  it('reads a stream the API cut short as a conflict', async () => {
    const { api, files } = setup()
    await files.write(ws, 'a.bin', Buffer.from('abcdef'))
    api.cut = true
    const parts: Buffer[] = []
    await expect(
      (async () => {
        for await (const part of files.chunks(ws, 'a.bin', sha('abcdef'))) parts.push(part)
      })(),
    ).rejects.toMatchObject({ code: 'conflict' })
  })

  it('refuses a path that climbs before it sends anything', async () => {
    const { api, files } = setup()
    for (const path of ['../x', 'a/../b', '/etc/passwd', '.hidden']) {
      await expect(files.write(ws, path, Buffer.from('x'))).rejects.toMatchObject({ code: 'invalid_path' })
      await expect(files.stat(ws, path)).rejects.toMatchObject({ code: 'invalid_path' })
      await expect(files.delete(ws, path)).rejects.toMatchObject({ code: 'invalid_path' })
    }
    await expect(files.list(ws, '../up')).rejects.toBeInstanceOf(WorkspaceError)
    expect(api.seen).toEqual([])
  })

  it('sends a workspace and a path in the query, encoded, never in the route path', async () => {
    const { api, files } = setup()
    const principal: WorkspaceRef = { kind: 'principal', principalKind: 'oidc', principalId: 'https://idp.example/realm#sub/1' }
    await files.stat(principal, 'a.txt')
    const seen = api.seen.at(-1)!
    expect(seen.path).toBe('/workspace/file')
    expect(seen.query.get('workspace')).toBe('principal:oidc:https://idp.example/realm#sub/1')
    expect(seen.query.get('path')).toBe('a.txt')
  })

  it('refuses a file past the cap without sending it', async () => {
    const { api, files } = setup()
    await expect(files.write(ws, 'big', Buffer.alloc(2048))).rejects.toMatchObject({ code: 'too_large' })
    expect(api.seen).toEqual([])
  })

  it('deletes with the events and the expected sha256', async () => {
    const { api, files } = setup()
    await files.write(ws, 'a', Buffer.from('x'))
    const committed: number[][] = []
    const deleted = await files.delete(ws, 'a', {
      expectedSha256: sha('x'),
      log: { events: [uploaded('a')], committed: (_e, seqs) => committed.push([...seqs]) },
    })
    expect(deleted).toBe(true)
    expect(api.seen.at(-1)!.body).toMatchObject({ workspace: `session:${SESSION}`, path: 'a', expected_sha256: sha('x') })
    expect(committed).toEqual([[1]])
    expect(await files.delete(ws, 'a')).toBe(false)
  })

  it('moves to blobs until a batch moves nothing, and reconciles, sweeps and copies through the routes', async () => {
    const { api, files } = setup()
    expect(await files.moveToBlobs()).toEqual({ moved: 4, local: false })
    expect(await files.reconcile(3_600_000)).toEqual({ released: 0, held: 0, skipped: 'held' })
    expect(api.seen.at(-1)!.body).toEqual({ grace_seconds: 3600 })
    expect(await files.sweep()).toBe(1)
    await files.copy(SESSION, '1b6d8a52-3a4e-4d36-9a3c-2f8c1d1e0f11')
    expect(await files.bytesStatus()).toEqual({ store: 'blobs', backend: 's3' })
    expect(await files.sealedUnreadable()).toBeUndefined()
  })

  it("takes a proxy's answer as no answer, never as a refusal of the file", async () => {
    const { api, files } = setup()
    api.proxy = true
    const err = await files.stat(ws, 'a').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(CrudReadError)
    expect(err).not.toBeInstanceOf(WorkspaceError)
    expect((await files.bytesStatus()).store).toBe('refused')
  })

  it('goes over SQL until the API lists the area, then through the routes', async () => {
    const { api, files } = setup()
    const calls: string[] = []
    const pg = {
      maxFiles: 5000,
      stat: async () => {
        calls.push('pg')
        return undefined
      },
    } as unknown as SessionFiles
    let listed = false
    const gated = gatedWorkspaceFiles(pg, files, () => listed)
    await gated.stat(ws, 'a')
    listed = true
    await gated.stat(ws, 'a')
    expect(calls).toEqual(['pg'])
    expect(api.seen.map((s) => s.path)).toEqual(['/workspace/file'])
  })
})
