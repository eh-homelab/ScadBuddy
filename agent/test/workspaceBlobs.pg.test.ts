import { createHash, randomBytes } from 'node:crypto'
import { createServer, type IncomingMessage, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { kekFromBase64, SealError } from '../src/secrets.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { SubjectForgotten } from '../src/temporal/payloadCodec.js'
import { BackendWorkspaceBlobs, WORKSPACE_BLOBS } from '../src/workspace/blobs.js'
import { rewrapWorkspaceKeys, WorkspaceSealing } from '../src/workspace/sealing.js'
import { SessionFiles, sha256, type WorkspaceRef } from '../src/workspace/store.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #4789: a workspace file's bytes are a blob of the backend's content store, sealed
// under a key of the file's own, which is wrapped under the session's data key (a
// principal's: the KEK). The backend here is a stand-in for its hidden routes
// (backend api/internal_workspace.py, api/internal_blobs.py), which mint the URLs:
// the store reaches it only through BackendWorkspaceBlobs, as in production.

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
    (await db.sql<{ key: string; holder_id: string }[]>`
      SELECT key, holder_id FROM blob_refs WHERE holder_kind = 'ai_session_file' ORDER BY holder_id`).map((r) => [r.key, r.holder_id])
  const all = async (it: AsyncIterable<Buffer>) => {
    const parts: Buffer[] = []
    for await (const part of it) parts.push(part)
    return Buffer.concat(parts)
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    // The backend's own table (backend migrations 20260928T0900Z, 20261007T0900Z), in the one database both use.
    await db.sql`
      CREATE TABLE IF NOT EXISTS blob_refs (
        key text NOT NULL, holder_kind text NOT NULL, holder_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (key, holder_kind, holder_id))`
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
    // Forgetting the parent leaves the fork's copy whole.
    await forgetSubject({ sql: db.sql }, `session-${sessionId}`)
    expect((await files.read(childWs, 'a.txt'))?.bytes.toString()).toBe('from the parent')
    expect(await holders()).toEqual([[parent!.blob_key, `session:${child.id}/a.txt`]])
  })

  it("leaves a forgotten session's blob unopenable, and drops its holder", async () => {
    await files.write(ws, 'secret.txt', Buffer.from('forget me'))
    const kept = (await row('secret.txt'))!
    await forgetSubject({ sql: db.sql, keys: testPayloadKeys(db.sql) }, `session-${sessionId}`)
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
      await refusedWith(files, /could not be reached/)
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
})
