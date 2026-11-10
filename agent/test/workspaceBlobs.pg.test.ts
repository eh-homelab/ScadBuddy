import { createHash, randomBytes } from 'node:crypto'
import { createServer, type IncomingMessage, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database, LockClient, LockLease } from '../src/db.js'
import { kekFromBase64, SealError } from '../src/secrets.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { payloadKeyLock, SubjectForgotten } from '../src/temporal/payloadCodec.js'
import { BackendWorkspaceBlobs, WORKSPACE_BLOB_REFS, WORKSPACE_BLOBS } from '../src/workspace/blobs.js'
import { rewrapWorkspaceKeys, WorkspaceSealing } from '../src/workspace/sealing.js'
import { SessionFiles, sha256, type WorkspaceRef } from '../src/workspace/store.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #4789: a workspace file's bytes are a blob of the backend's content store, sealed
// under a key of the file's own, which is wrapped under the session's data key (a
// principal's: the KEK). The backend here is a stand-in for its hidden routes
// (backend api/internal_workspace.py, api/internal_blobs.py), which mint the URLs:
// the store reaches it only through BackendWorkspaceBlobs, as in production. Its holds
// stand in for the backend's `blob_refs` rows of kind `ai_session_file`, which the agent
// writes and drops only through its CRUD routes (backend api/crud_agent.py, #5299).

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const MIB = 1024 * 1024

/**
 * The backend's routes over a Map. `storeBackend` is what its /healthz says; on `local` every
 * workspace route answers 404, as the backend's does. `routes` is how they answer on s3:
 * `unavailable` as without SCADBUDDY_API_INTERNAL_URL or the KEK (503), `missing` as a backend
 * older than #4789 (FastAPI's own 404).
 */
class FakeBackend {
  readonly blobs = new Map<string, Buffer>()
  readonly pending = new Map<string, Buffer>()
  puts = 0
  storeBackend: 'local' | 's3' = 's3'
  routes: 'ok' | 'unavailable' | 'missing' = 'ok'
  /** `blob_refs`' `ai_session_file` rows: `<key> <holder>` → when held (ms). */
  readonly holds = new Map<string, number>()
  /** Every hold and release the agent sent, in order: `hold <key> <holder>`, `release …`. */
  readonly refCalls: string[] = []
  /** How the hold and release routes answer: `refused` as a 403. */
  refs: 'ok' | 'refused' = 'ok'
  server!: Server
  base = ''

  async start(): Promise<void> {
    this.server = createServer((req, res) => {
      void this.handle(req).then(
        ({ status, body, type }) => {
          res.writeHead(status, type ? { 'content-type': type } : {})
          res.end(body)
        },
        (err: unknown) => {
          res.writeHead(500)
          res.end(String(err))
        },
      )
    })
    await new Promise<void>((resolve) => this.server.listen(0, '127.0.0.1', resolve))
    this.base = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`
  }

  async stop(): Promise<void> {
    await new Promise((resolve) => this.server.close(resolve))
  }

  async handle(req: IncomingMessage): Promise<{ status: number; body?: string | Buffer; type?: string }> {
    const chunks: Buffer[] = []
    for await (const chunk of req) chunks.push(chunk as Buffer)
    const body = Buffer.concat(chunks)
    const url = new URL(req.url ?? '/', this.base)
    const json = (value: unknown) => ({ status: 200, body: JSON.stringify(value), type: 'application/json' })
    const notFound = { status: 404, body: JSON.stringify({ detail: 'store_backend local has no blob store' }), type: 'application/problem+json' }
    const expires_at = new Date(Date.now() + 3600_000).toISOString()
    if (req.method === 'GET' && url.pathname === '/healthz') return json({ status: 'ok', store: { backend: this.storeBackend } })
    if (url.pathname.startsWith(WORKSPACE_BLOBS)) {
      if (this.storeBackend === 'local') return notFound
      if (this.routes === 'missing') return { status: 404, body: JSON.stringify({ detail: 'Not Found' }), type: 'application/json' }
      if (this.routes === 'unavailable') {
        return { status: 503, body: JSON.stringify({ detail: 'the native slicer needs SCADBUDDY_API_INTERNAL_URL' }), type: 'application/problem+json' }
      }
    }
    if (url.pathname.startsWith(WORKSPACE_BLOB_REFS)) return this.refsRoute(req.method ?? 'GET', url, body)
    if (req.method === 'POST' && url.pathname === `${WORKSPACE_BLOBS}/uploads`) {
      const pending = `pending-workspace-${randomBytes(16).toString('hex')}`
      return json({ pending_key: pending, url: { url: `${this.base}/blob/${pending}`, headers: { 'X-ScadBuddy-Transfer': 'tok' }, expires_at } })
    }
    const finish = new RegExp(`^${WORKSPACE_BLOBS}/uploads/(pending-workspace-[0-9a-f]{32})/finish$`).exec(url.pathname)
    if (req.method === 'POST' && finish) {
      const put = this.pending.get(finish[1]!)
      const { sha256: digest, size } = JSON.parse(body.toString()) as { sha256: string; size: number }
      if (!put || createHash('sha256').update(put).digest('hex') !== digest || put.length !== size) return { status: 409 }
      const key = `workspace-${digest}`
      this.blobs.set(key, put)
      return json({ key })
    }
    const get = new RegExp(`^${WORKSPACE_BLOBS}/(workspace-[0-9a-f]{64})/url$`).exec(url.pathname)
    if (req.method === 'GET' && get) {
      if (!this.blobs.has(get[1]!)) return notFound
      return json({ url: `${this.base}/blob/${get[1]}`, headers: {}, expires_at })
    }
    const blob = /^\/blob\/(.+)$/.exec(url.pathname)
    if (blob && req.method === 'PUT') {
      if (req.headers['x-scadbuddy-transfer'] !== 'tok') return { status: 403 }
      this.puts += 1
      this.pending.set(blob[1]!, body)
      return { status: 204 }
    }
    if (blob && req.method === 'GET') {
      const data = this.blobs.get(blob[1]!)
      return data ? { status: 200, body: data, type: 'application/octet-stream' } : { status: 404 }
    }
    return { status: 404 }
  }

  /** backend api/crud_agent.py's holds over `holds`. */
  refsRoute(method: string, url: URL, body: Buffer): { status: number; body?: string; type?: string } {
    const json = (value: unknown) => ({ status: 200, body: JSON.stringify(value), type: 'application/json' })
    if (this.refs === 'refused') return { status: 403, body: JSON.stringify({ detail: 'not this credential' }), type: 'application/problem+json' }
    if (method === 'POST') {
      const { key, holder_id: holder, older_than_seconds: older } = JSON.parse(body.toString()) as { key: string; holder_id: string; older_than_seconds?: number }
      const id = `${key} ${holder}`
      if (url.pathname === WORKSPACE_BLOB_REFS) {
        this.refCalls.push(`hold ${id}`)
        this.holds.set(id, Date.now())
        return { status: 204 }
      }
      this.refCalls.push(`release ${id}`)
      const at = this.holds.get(id)
      const released = at !== undefined && (older === undefined || at <= Date.now() - older * 1000)
      if (released) this.holds.delete(id)
      return json({ released })
    }
    const older = Number(url.searchParams.get('older_than_seconds') ?? 0)
    const limit = Number(url.searchParams.get('limit') ?? 500)
    const after = url.searchParams.get('after_holder_id')
    const afterKey = url.searchParams.get('after_key') ?? ''
    const listed = [...this.holds]
      .filter(([, at]) => at <= Date.now() - older * 1000)
      .map(([id]) => {
        const [key, holder_id] = [id.slice(0, id.indexOf(' ')), id.slice(id.indexOf(' ') + 1)]
        return { key: key!, holder_id: holder_id! }
      })
      .sort((a, b) => a.holder_id.localeCompare(b.holder_id) || a.key.localeCompare(b.key))
      .filter((h) => after === null || h.holder_id > after || (h.holder_id === after && h.key > afterKey))
      .slice(0, limit)
    return json({ holds: listed, next: listed.length === limit ? listed.at(-1) : null })
  }
}

const kek = kekFromBase64(Buffer.alloc(32, 9).toString('base64'))
const nextKek = kekFromBase64(Buffer.alloc(32, 10).toString('base64'))

describe.skipIf(skip !== undefined)(`workspace files in the blob store${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let backend: FakeBackend
  let sealing: WorkspaceSealing
  let files: SessionFiles
  let inline: SessionFiles
  let sessionId: string
  let ws: WorkspaceRef
  let m: ReturnType<typeof manager>

  const filesWith = (limits: { fileMaxBytes: number; workspaceMaxBytes: number }, k: typeof kek | null = kek) =>
    new SessionFiles(db.sql, {
      limits,
      blobs: {
        // No cache: a test changes what the backend says between calls.
        store: new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 }),
        ...(k ? { sealing: new WorkspaceSealing(testPayloadKeys(db.sql), k) } : {}),
      },
    })
  const row = async (path: string, workspace = `session:${sessionId}`) => {
    const [r] = await db.sql<{ data: Buffer | null; blob_key: string | null; key_sealed: Buffer | null; key_kek_id: string | null }[]>`
      SELECT data, blob_key, key_sealed, key_kek_id FROM ai_session_files WHERE workspace = ${workspace} AND path = ${path}`
    return r
  }
  const holders = async () =>
    [...backend.holds.keys()]
      .map((id) => [id.slice(0, id.indexOf(' ')), id.slice(id.indexOf(' ') + 1)])
      .sort((a, b) => a[1]!.localeCompare(b[1]!) || a[0]!.localeCompare(b[0]!))
  const all = async (it: AsyncIterable<Buffer>) => {
    const parts: Buffer[] = []
    for await (const part of it) parts.push(part)
    return Buffer.concat(parts)
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    backend = new FakeBackend()
    await backend.start()
    sealing = new WorkspaceSealing(testPayloadKeys(db.sql), kek)
    files = filesWith({ fileMaxBytes: 4 * MIB, workspaceMaxBytes: 8 * MIB })
    inline = new SessionFiles(db.sql, { limits: { fileMaxBytes: 4 * MIB, workspaceMaxBytes: 8 * MIB } })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    ;({
      session: { id: sessionId },
    } = await m.start(browser, { origin: 'chat' }))
    ws = { kind: 'session', sessionId }
  })
  afterEach(async () => {
    await backend.stop()
    await drop()
  })

  it('writes, reads, streams and deletes a file whose bytes are a sealed blob held while its row stands', async () => {
    const bytes = Buffer.from('hello, blob store')
    const info = await files.write(ws, 'notes/a.txt', bytes)
    expect(info).toMatchObject({ size: bytes.length, sha256: sha256(bytes), mediaType: 'text/plain' })
    const stored = await row('notes/a.txt')
    expect(stored?.data).toBeNull()
    expect(stored?.blob_key).toMatch(/^workspace-[0-9a-f]{64}$/)
    const blob = backend.blobs.get(stored!.blob_key!)!
    expect(stored!.blob_key).toBe(`workspace-${createHash('sha256').update(blob).digest('hex')}`)
    // Sealed: the store never sees the bytes.
    expect(blob.includes(bytes)).toBe(false)
    expect(await holders()).toEqual([[stored!.blob_key, `session:${sessionId}/notes/a.txt`]])
    expect((await files.read(ws, 'notes/a.txt'))?.bytes.toString()).toBe('hello, blob store')
    expect((await all(files.chunks(ws, 'notes/a.txt', info.sha256))).toString()).toBe('hello, blob store')
    await expect(all(files.chunks(ws, 'notes/a.txt', 'f'.repeat(64)))).rejects.toMatchObject({ code: 'conflict' })
    // A replacement holds its own blob; the old one's holder goes with it.
    await files.write(ws, 'notes/a.txt', Buffer.from('two'))
    const replaced = await row('notes/a.txt')
    expect(replaced?.blob_key).not.toBe(stored?.blob_key)
    expect(await holders()).toEqual([[replaced!.blob_key, `session:${sessionId}/notes/a.txt`]])
    expect(await files.delete(ws, 'notes/a.txt')).toBe(true)
    expect(await holders()).toEqual([])
  })

  it('streams a file of several segments, and refuses one cut short or altered', async () => {
    const bytes = randomBytes(2 * MIB + 12345)
    const info = await files.write(ws, 'big.bin', bytes)
    const parts: Buffer[] = []
    for await (const part of files.chunks(ws, 'big.bin', info.sha256)) parts.push(part)
    expect(parts.length).toBe(3)
    expect(Buffer.concat(parts).equals(bytes)).toBe(true)
    expect((await files.read(ws, 'big.bin'))?.bytes.equals(bytes)).toBe(true)
    // An exact multiple of the segment ends on a full segment.
    const even = randomBytes(2 * MIB)
    await files.write(ws, 'even.bin', even)
    expect((await files.read(ws, 'even.bin'))?.bytes.equals(even)).toBe(true)
    const empty = await files.write(ws, 'empty', Buffer.alloc(0))
    expect((await files.read(ws, 'empty'))?.bytes.length).toBe(0)
    expect(await all(files.chunks(ws, 'empty', empty.sha256))).toHaveLength(0)
    const key = (await row('big.bin'))!.blob_key!
    const blob = backend.blobs.get(key)!
    // Cut at a segment's end: the segment left last was not sealed as the last.
    backend.blobs.set(key, blob.subarray(0, MIB + 29))
    await expect(files.read(ws, 'big.bin')).rejects.toBeInstanceOf(SealError)
    const altered = Buffer.from(blob)
    altered[100] = altered[100]! ^ 1
    backend.blobs.set(key, altered)
    await expect(files.read(ws, 'big.bin')).rejects.toBeInstanceOf(SealError)
  })

  it('refuses an over-limit write before any upload', async () => {
    const small = filesWith({ fileMaxBytes: 100, workspaceMaxBytes: 150 })
    await expect(small.write(ws, 'a', Buffer.alloc(101))).rejects.toMatchObject({ code: 'too_large' })
    await small.write(ws, 'a', Buffer.alloc(100))
    const before = backend.puts
    await expect(small.write(ws, 'b', Buffer.alloc(51))).rejects.toMatchObject({ code: 'quota' })
    await expect(small.write(ws, 'a', Buffer.alloc(1), { expectedSha256: null })).rejects.toMatchObject({ code: 'conflict' })
    expect(backend.puts).toBe(before)
  })

  it("forks by holding the parent's blob under a key wrapped for the child: no bytes move", async () => {
    await files.write(ws, 'a.txt', Buffer.from('from the parent'))
    const { session: child } = await m.start(browser, { origin: 'chat' })
    const puts = backend.puts
    await files.copy(sessionId, child.id)
    expect(backend.puts).toBe(puts)
    const parent = await row('a.txt')
    const forked = await row('a.txt', `session:${child.id}`)
    expect(forked?.blob_key).toBe(parent?.blob_key)
    expect(forked?.key_sealed?.equals(parent!.key_sealed!)).toBe(false)
    expect(await holders()).toEqual([
      [parent!.blob_key, `session:${child.id}/a.txt`],
      [parent!.blob_key, `session:${sessionId}/a.txt`],
    ].sort((x, y) => x[1]!.localeCompare(y[1]!)))
    const childWs: WorkspaceRef = { kind: 'session', sessionId: child.id }
    expect((await files.read(childWs, 'a.txt'))?.bytes.toString()).toBe('from the parent')
    // Forgetting the parent leaves the fork's copy whole; reconcile releases the parent's hold.
    await forgetSubject({ sql: db.sql }, `session-${sessionId}`)
    expect((await files.read(childWs, 'a.txt'))?.bytes.toString()).toBe('from the parent')
    expect(await files.reconcile(0)).toEqual({ released: 1, held: 0 })
    expect(await holders()).toEqual([[parent!.blob_key, `session:${child.id}/a.txt`]])
  })

  it("leaves a forgotten session's blob unopenable, and drops its holder", async () => {
    await files.write(ws, 'secret.txt', Buffer.from('forget me'))
    const kept = (await row('secret.txt'))!
    await forgetSubject({ sql: db.sql, keys: testPayloadKeys(db.sql) }, `session-${sessionId}`)
    // The rows went in a cascade, which tells the backend nothing: reconcile releases the hold.
    expect(await files.reconcile(0)).toEqual({ released: 1, held: 0 })
    expect(await holders()).toEqual([])
    expect(backend.blobs.has(kept.blob_key!)).toBe(true) // until the backend's sweep
    await expect(
      sealing.unwrap({ kind: 'session', sessionId }, 'secret.txt', kept.blob_key!, kept.key_sealed!, kept.key_kek_id),
    ).rejects.toBeInstanceOf(SubjectForgotten)
  })

  it("keeps the bytes in Postgres when the backend has no blob store (store_backend local)", async () => {
    backend.storeBackend = 'local'
    await files.write(ws, 'a.txt', Buffer.from('inline'))
    const stored = await row('a.txt')
    expect(stored?.data?.toString()).toBe('inline')
    expect(stored?.blob_key).toBeNull()
    expect((await files.read(ws, 'a.txt'))?.bytes.toString()).toBe('inline')
    expect(await holders()).toEqual([])
  })

  it("seals a principal's file under the KEK, re-wrapped at a rotation", async () => {
    const principal: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: 'token:t1' }
    await files.write(principal, 'p.txt', Buffer.from('mine'))
    expect((await row('p.txt', 'principal:bearer:token:t1'))?.key_kek_id).toBe(kek.id)
    expect(await rewrapWorkspaceKeys(db.sql, kek, nextKek)).toEqual({ rewrapped: 1, failed: 0 })
    expect((await row('p.txt', 'principal:bearer:token:t1'))?.key_kek_id).toBe(nextKek.id)
    const rotated = filesWith({ fileMaxBytes: 4 * MIB, workspaceMaxBytes: 8 * MIB }, nextKek)
    expect((await rotated.read(principal, 'p.txt'))?.bytes.toString()).toBe('mine')
    await rotated.sweep(0)
    expect(await holders()).toEqual([])
  })

  it('moves the files kept in Postgres to blobs, idempotently', async () => {
    const principal: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: 'token:t1' }
    const big = randomBytes(MIB + 7)
    await inline.write(ws, 'a.txt', Buffer.from('one'))
    await inline.write(ws, 'dir/big.bin', big)
    await inline.write(principal, 'p.txt', Buffer.from('mine'))
    expect(await files.moveToBlobs()).toEqual({ moved: 3, local: false })
    expect(await files.moveToBlobs()).toEqual({ moved: 0, local: false })
    const rows = await db.sql<{ data: Buffer | null; blob_key: string | null }[]>`SELECT data, blob_key FROM ai_session_files`
    expect(rows.every((r) => r.data === null && r.blob_key !== null)).toBe(true)
    expect((await files.read(ws, 'a.txt'))?.bytes.toString()).toBe('one')
    expect((await files.read(ws, 'dir/big.bin'))?.bytes.equals(big)).toBe(true)
    expect((await files.read(principal, 'p.txt'))?.bytes.toString()).toBe('mine')
    expect(await holders()).toHaveLength(3)
    // Without a blob store it moves nothing and says why.
    await inline.write(ws, 'later.txt', Buffer.from('x'))
    backend.storeBackend = 'local'
    expect(await files.moveToBlobs()).toEqual({ moved: 0, local: true })
    expect(await files.bytesStatus()).toMatchObject({ store: 'postgres' })
  })
  describe('on a backend with a blob store (store_backend s3), never in Postgres', () => {
    const refusedWith = async (target: SessionFiles, cause: RegExp) => {
      const error = await target.write(ws, 'a.txt', Buffer.from('plain secret')).catch((err: unknown) => err)
      expect(error).toMatchObject({ code: 'unavailable' })
      expect((error as Error).message).toMatch(cause)
      expect((error as Error).message).toMatch(/nothing was written/)
      expect(await row('a.txt')).toBeUndefined()
      const status = await target.bytesStatus()
      expect(status).toMatchObject({ store: 'refused', reason: expect.stringMatching(cause) })
    }

    it('refuses a write when this agent has no KEK to seal it with', async () => {
      await refusedWith(filesWith({ fileMaxBytes: 4 * MIB, workspaceMaxBytes: 8 * MIB }, null), /SCADBUDDY_SECRET_KEY_FILE/)
      expect(backend.puts).toBe(0)
    })

    it('refuses a write when the backend cannot mint transfer URLs (no internal URL or KEK)', async () => {
      backend.routes = 'unavailable'
      await refusedWith(files, /SCADBUDDY_API_INTERNAL_URL/)
    })

    it('refuses a write when the backend is older than its workspace blob routes', async () => {
      backend.routes = 'missing'
      await refusedWith(files, /older than #4789/)
    })

    it('refuses a write when the backend cannot be reached to say where files go', async () => {
      await backend.stop()
      await refusedWith(files, /the backend is not up yet \(\/healthz\)/)
      await backend.start()
    })

    it('refuses to move files out of Postgres, and leaves them as they are', async () => {
      await inline.write(ws, 'a.txt', Buffer.from('one'))
      backend.routes = 'unavailable'
      await expect(files.moveToBlobs()).rejects.toMatchObject({ code: 'unavailable' })
      expect((await row('a.txt'))?.data?.toString()).toBe('one')
      const keyless = filesWith({ fileMaxBytes: 4 * MIB, workspaceMaxBytes: 8 * MIB }, null)
      await expect(keyless.moveToBlobs()).rejects.toMatchObject({ code: 'unavailable' })
    })

    it('says the bytes go to the blob store when every part is there', async () => {
      expect(await files.bytesStatus()).toEqual({ store: 'blobs', backend: 's3' })
    })
  })
  describe('review of e19ae8ff6', () => {
    it('leaves no half-made child when a fork cannot copy its files', async () => {
      await files.write(ws, 'a.txt', Buffer.from('sealed'))
      await inline.write(ws, 'b.txt', Buffer.from('inline'))
      // An agent that cannot open the parent's file key (no KEK) cannot fork its files.
      const keyless = filesWith({ fileMaxBytes: 4 * MIB, workspaceMaxBytes: 8 * MIB }, null)
      const forking = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner, workspace: keyless })
      await forking.store.append({ projectKey: 'p', sessionId }, [{ type: 'user', uuid: 'u1', message: {} }])
      const sessionsBefore = await db.sql`SELECT id FROM ai_sessions`
      await expect(forking.fork(sessionId, browser)).rejects.toMatchObject({ code: 'unavailable' })
      expect(await db.sql`SELECT id FROM ai_sessions`).toHaveLength(sessionsBefore.length)
      const workspaces = await db.sql<{ workspace: string }[]>`SELECT DISTINCT workspace FROM ai_session_files`
      expect(workspaces.map((w) => w.workspace)).toEqual([`session:${sessionId}`])
      const entries = await db.sql<{ session_id: string }[]>`SELECT DISTINCT session_id::text FROM ai_session_entries`
      expect(entries.map((e) => e.session_id)).toEqual([sessionId])
    })

    it("never makes a payload key for a session that is gone or being forgotten", async () => {
      const subject = `session-${sessionId}`
      // forgetSubject has deleted the key (and left its tombstone) but not yet the rows.
      await forgetSubject({ sql: db.sql }, subject).catch(() => undefined)
      await expect(files.write(ws, 'late.txt', Buffer.from('late'))).rejects.toThrow()
      expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(0)
      // A session deleted another way (no tombstone): no key either.
      const { session: other } = await m.start(browser, { origin: 'chat' })
      await db.sql`DELETE FROM ai_payload_keys WHERE subject = ${`session-${other.id}`}`
      await db.sql`DELETE FROM ai_sessions WHERE id = ${other.id}`
      await expect(files.write({ kind: 'session', sessionId: other.id }, 'late.txt', Buffer.from('late'))).rejects.toThrow()
      expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${`session-${other.id}`}`).toHaveLength(0)
    })

    it('says how many files cannot be read after a switch back to store_backend local', async () => {
      await files.write(ws, 'a.txt', Buffer.from('sealed'))
      await files.write(ws, 'b.txt', Buffer.from('sealed too'))
      backend.storeBackend = 'local'
      expect(await files.bytesStatus()).toMatchObject({ store: 'postgres', unreadable_files: 2 })
      await expect(files.read(ws, 'a.txt')).rejects.toMatchObject({ code: 'unavailable' })
      // New writes are kept in Postgres and read back.
      await files.write(ws, 'c.txt', Buffer.from('inline'))
      expect((await files.read(ws, 'c.txt'))?.bytes.toString()).toBe('inline')
    })
  })
  describe('review of dd9b49be5', () => {
    it("removes a failed fork's payload key with its row", async () => {
      await files.write(ws, 'a.txt', Buffer.from('one'))
      await files.write(ws, 'b.txt', Buffer.from('two'))
      // The second file key cannot be opened: the copy fails after it wrapped the first for the child.
      await db.sql`UPDATE ai_session_files SET key_sealed = ${Buffer.alloc(61, 1)} WHERE path = 'b.txt'`
      const forking = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner, workspace: files })
      await forking.store.append({ projectKey: 'p', sessionId }, [{ type: 'user', uuid: 'u1', message: {} }])
      await expect(forking.fork(sessionId, browser)).rejects.toBeInstanceOf(SealError)
      const orphans = await db.sql`
        SELECT subject FROM ai_payload_keys k
        WHERE NOT EXISTS (SELECT 1 FROM ai_sessions s WHERE 'session-' || s.id::text = k.subject)`
      expect(orphans).toHaveLength(0)
    })

    it('makes a payload key only for a session whose row stands, in the one locked insert', async () => {
      const keys = testPayloadKeys(db.sql)
      const gone = 'session-00000000-0000-4000-8000-000000000000'
      await expect(keys.keyFor(gone, true, { onlyForStandingSession: true })).rejects.toBeInstanceOf(SubjectForgotten)
      expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${gone}`).toHaveLength(0)
      expect((await keys.keyFor(`session-${sessionId}`, true, { onlyForStandingSession: true })).length).toBe(32)
    })

    it('never throws from bytesStatus, even when the count cannot be read', async () => {
      backend.storeBackend = 'local'
      await db.sql`ALTER TABLE ai_session_files RENAME TO ai_session_files_away`
      try {
        expect(await files.bytesStatus()).toEqual({ store: 'postgres', reason: 'the backend has no blob store (store_backend local)' })
      } finally {
        await db.sql`ALTER TABLE ai_session_files_away RENAME TO ai_session_files`
      }
    })
  })
  describe('review of 10b6be073', () => {
    it("deletes a failed fork's row under its key's lock, so no key creation reads it as standing", async () => {
      const { session: child } = await m.start(browser, { origin: 'chat' })
      const subject = `session-${child.id}`
      let release!: () => void
      const held = new Promise<void>((resolve) => (release = resolve))
      let locked!: () => void
      const isLocked = new Promise<void>((resolve) => (locked = resolve))
      await db.sql`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
      // A key creation for the child, mid-insert: it holds the lock, has seen the row and
      // inserted its key, as keyFor's insert does, and has not committed yet.
      const creation = db.sql.begin(async (tx) => {
        await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
        const inserted = await tx`
          INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)
          SELECT ${subject}, ${Buffer.alloc(61, 2)}, 'test'
          WHERE EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = substr(${subject}, 9))
          RETURNING subject`
        expect(inserted).toHaveLength(1)
        locked()
        await held
      })
      await isLocked
      const dropping = (m as unknown as { dropFailedFork(id: string): Promise<void> }).dropFailedFork(child.id)
      try {
        await new Promise((resolve) => setTimeout(resolve, 300))
        expect(await db.sql`SELECT 1 FROM ai_sessions WHERE id = ${child.id}`).toHaveLength(1)
      } finally {
        release()
        await creation
        await dropping
      }
      expect(await db.sql`SELECT 1 FROM ai_sessions WHERE id = ${child.id}`).toHaveLength(0)
      // The key the creation committed went with the row.
      expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(0)
    })
  })
  describe('holds through the CRUD routes, not a trigger (#5299)', () => {
    it('holds a blob before the row that names it is written', async () => {
      let atHold: unknown = 'not held'
      const hold = BackendWorkspaceBlobs.prototype.hold
      const store = new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 })
      store.hold = async function (key: string, holder: string) {
        atHold = (await row('a.txt')) ?? null
        return hold.call(this, key, holder)
      }
      const watched = new SessionFiles(db.sql, { blobs: { store, sealing } })
      await watched.write(ws, 'a.txt', Buffer.from('held first'))
      expect(atHold).toBeNull()
      const stored = await row('a.txt')
      expect(backend.refCalls).toEqual([`hold ${stored!.blob_key} session:${sessionId}/a.txt`])
    })

    it('fails a write the backend refuses to hold, and writes no row', async () => {
      backend.refs = 'refused'
      const error = await files.write(ws, 'a.txt', Buffer.from('unheld')).catch((err: unknown) => err)
      expect(error).toMatchObject({ code: 'unavailable' })
      expect((error as Error).message).toMatch(/refused the workspace blob hold \(HTTP 403/)
      expect((error as Error).message).toMatch(/nothing was written/)
      expect(await row('a.txt')).toBeUndefined()
    })

    it('releases the old blob after a replacement, a delete and a sweep', async () => {
      await files.write(ws, 'a.txt', Buffer.from('one'))
      const first = (await row('a.txt'))!.blob_key!
      await files.write(ws, 'a.txt', Buffer.from('two'))
      const second = (await row('a.txt'))!.blob_key!
      const holder = `session:${sessionId}/a.txt`
      expect(backend.refCalls).toEqual([`hold ${first} ${holder}`, `hold ${second} ${holder}`, `release ${first} ${holder}`])
      expect(await holders()).toEqual([[second, holder]])
      expect(await files.delete(ws, 'a.txt')).toBe(true)
      expect(backend.refCalls.at(-1)).toBe(`release ${second} ${holder}`)
      expect(await holders()).toEqual([])
      const principal: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: 'token:t1' }
      await files.write(principal, 'p.txt', Buffer.from('mine'))
      const swept = (await row('p.txt', 'principal:bearer:token:t1'))!.blob_key!
      expect(await files.sweep(0)).toBe(1)
      expect(backend.refCalls.at(-1)).toBe(`release ${swept} principal:bearer:token:t1/p.txt`)
      expect(await holders()).toEqual([])
    })

    it('deletes the row when a release fails, and reconcile releases the hold later', async () => {
      await files.write(ws, 'a.txt', Buffer.from('one'))
      const key = (await row('a.txt'))!.blob_key!
      backend.refs = 'refused'
      expect(await files.delete(ws, 'a.txt')).toBe(true)
      expect(await holders()).toEqual([[key, `session:${sessionId}/a.txt`]])
      backend.refs = 'ok'
      expect(await files.reconcile(0)).toEqual({ released: 1, held: 0 })
      expect(await holders()).toEqual([])
    })

    it('releases an orphan hold older than the grace, and keeps a younger one', async () => {
      const old = `workspace-${'1'.repeat(64)}`
      const young = `workspace-${'2'.repeat(64)}`
      backend.holds.set(`${old} session:${sessionId}/gone.txt`, Date.now() - 2 * 3600_000)
      backend.holds.set(`${young} session:${sessionId}/coming.txt`, Date.now())
      await files.write(ws, 'kept.txt', Buffer.from('kept'))
      const kept = (await row('kept.txt'))!.blob_key!
      // An hour's grace: only the old orphan goes; the young one may be a write about to land.
      expect(await files.reconcile(3600_000)).toEqual({ released: 1, held: 0 })
      expect(await holders()).toEqual([
        [young, `session:${sessionId}/coming.txt`],
        [kept, `session:${sessionId}/kept.txt`],
      ])
    })

    it('releases a hold whose row names another blob, and holds a row no hold names', async () => {
      await files.write(ws, 'a.txt', Buffer.from('one'))
      const key = (await row('a.txt'))!.blob_key!
      const stale = `workspace-${'3'.repeat(64)}`
      backend.holds.set(`${stale} session:${sessionId}/a.txt`, Date.now() - 2 * 3600_000)
      // A row whose hold was lost (written by an older pod after the trigger was dropped, say).
      backend.holds.delete(`${key} session:${sessionId}/a.txt`)
      expect(await files.reconcile(3600_000)).toEqual({ released: 1, held: 1 })
      expect(await holders()).toEqual([[key, `session:${sessionId}/a.txt`]])
    })

    it('reconciles across pages of holds', async () => {
      for (let n = 0; n < 501; n += 1) backend.holds.set(`workspace-${n.toString(16).padStart(64, '0')} session:${sessionId}/f${n}`, 0)
      expect(await files.reconcile(0)).toEqual({ released: 501, held: 0 })
      expect(backend.holds.size).toBe(0)
    })

    it('holds nothing when the backend keeps the bytes in Postgres (store_backend local)', async () => {
      backend.storeBackend = 'local'
      await files.write(ws, 'a.txt', Buffer.from('inline'))
      expect(await files.delete(ws, 'a.txt')).toBe(true)
      expect(backend.refCalls).toEqual([])
    })
  })
  describe('review of 610034b0b', () => {
    it('reconciles on one replica at a time: a second caller skips while the lock is held', async () => {
      const orphan = `workspace-${'4'.repeat(64)}`
      backend.holds.set(`${orphan} session:${sessionId}/gone.txt`, 0)
      const replica = (store = new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 })) =>
        new SessionFiles(db.sql, { locks: db.locks, blobs: { store, sealing } })
      // The first replica's reconcile holds the lock while it lists the holds.
      let listing!: () => void
      const listed = new Promise<void>((resolve) => (listing = resolve))
      let finish!: () => void
      const finished = new Promise<void>((resolve) => (finish = resolve))
      const slow = new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 })
      const list = slow.listHolds.bind(slow)
      slow.listHolds = async (olderThanSeconds) => {
        listing()
        await finished
        return list(olderThanSeconds)
      }
      const first = replica(slow).reconcile(0)
      await listed
      const second = replica()
      expect(await second.reconcile(0)).toEqual({ released: 0, held: 0, skipped: 'held' })
      expect(backend.holds.size).toBe(1)
      finish()
      expect(await first).toEqual({ released: 1, held: 0 })
      // Once the lock is free again the next caller runs.
      backend.holds.set(`${orphan} session:${sessionId}/gone.txt`, 0)
      expect(await second.reconcile(0)).toEqual({ released: 1, held: 0 })
    })
  })
  describe('review of 9b86879ac', () => {
    const orphan = `workspace-${'5'.repeat(64)}`
    /** db.locks, except that the first `busy` takes find no free connection, and each lease's `done` is recorded. */
    const stubLocks = (busy: number, unlockFails = false) => {
      const done: boolean[] = []
      const locks: LockClient = {
        async take() {
          if (busy > 0) {
            busy -= 1
            return undefined
          }
          const lease = await db.locks.take()
          if (!lease) return undefined
          const sql = ((strings: TemplateStringsArray, ...values: unknown[]) => {
            if (unlockFails && strings.join('?').includes('pg_advisory_unlock')) return Promise.reject(new Error('connection lost'))
            return (lease.sql as unknown as (s: TemplateStringsArray, ...v: unknown[]) => Promise<unknown>)(strings, ...values)
          }) as unknown as LockLease['sql']
          return {
            sql,
            done(healthy) {
              done.push(healthy)
              // The real connection's lock is released either way, so later tests start clean.
              void lease.sql`SELECT pg_advisory_unlock_all()`.finally(() => lease.done(true))
            },
          }
        },
        end: () => Promise.resolve(),
      }
      return { locks, done }
    }

    it('skips with no_connection when no lock connection is free, and the sweeper tries again at its next tick', async () => {
      backend.holds.set(`${orphan} session:${sessionId}/gone.txt`, 0)
      const { locks } = stubLocks(1)
      const replica = new SessionFiles(db.sql, { locks, blobs: { store: new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 }), sealing } })
      expect(await replica.reconcile(0)).toEqual({ released: 0, held: 0, skipped: 'no_connection' })
      expect(backend.holds.size).toBe(1)
      // The sweeper's first tick finds no connection either; the next tick, not an hour later, reconciles.
      const { locks: sweeperLocks } = stubLocks(1)
      const swept = new SessionFiles(db.sql, {
        locks: sweeperLocks,
        blobs: { store: new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 }), sealing },
      })
      const stop = swept.startSweeper(50)
      try {
        await expect.poll(() => backend.holds.size, { timeout: 10_000 }).toBe(0)
      } finally {
        stop()
      }
    })

    it('retires a lock connection whose unlock fails', async () => {
      const { locks, done } = stubLocks(0, true)
      const replica = new SessionFiles(db.sql, { locks, blobs: { store: new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 }), sealing } })
      await expect(replica.reconcile(0)).rejects.toThrow('connection lost')
      expect(done).toEqual([false])
      // A clean run hands its connection back healthy.
      const clean = stubLocks(0)
      const next = new SessionFiles(db.sql, { locks: clean.locks, blobs: { store: new BackendWorkspaceBlobs(backend.base, { backendCacheMs: 0 }), sealing } })
      await expect.poll(async () => (await next.reconcile(0)).skipped ?? 'ran', { timeout: 5_000 }).toBe('ran')
      expect(clean.done.at(-1)).toBe(true)
    })
  })
})
