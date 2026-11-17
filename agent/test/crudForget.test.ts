import { randomBytes, randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { FORGET_AREAS } from '../src/api/crudAreas.js'
import type { Database } from '../src/db.js'
import { kekFromBase64, openBytes } from '../src/secrets.js'
import { sessionImage } from '../src/sessions/blobs.js'
import { CrudSubjectKeys, forgetSubject, gatedKeyDeletes, type KeyDeletes } from '../src/sessions/forget.js'
import { CrudFlowBlobs, type FlowBlobStore, flowBlobContext, gatedFlowBlobs } from '../src/temporal/flowBlobs.js'
import { type PayloadKeys, PgPayloadKeys, SubjectForgotten } from '../src/temporal/payloadCodec.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// Forgetting a subject, the key deletes no forget makes, and a flow run's images through the API's
// CRUD routes (#5299 step 6, area `forget`), against a fake API that keeps them in memory as the
// backend's api/crud_agent_forget.py does (the backend's tests/crud/test_crud_agent_forget.py
// covers the routes themselves).

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')

/** One data key per subject, as the API releases them; `forgotten` refuses one. */
class MemoryKeys implements PayloadKeys {
  readonly keys = new Map<string, Buffer>()
  readonly forgotten = new Set<string>()
  async keyFor(subject: string, create: boolean): Promise<Buffer> {
    if (this.forgotten.has(subject)) throw new SubjectForgotten(`${subject} was forgotten`)
    let key = this.keys.get(subject)
    if (!key && create) this.keys.set(subject, (key = randomBytes(32)))
    if (!key) throw new SubjectForgotten(`${subject} has no key`)
    return Buffer.from(key)
  }
}

type Stored = { mediaType: string; size: number; data: Buffer }

/** The API's side of area `forget`, in memory. */
class FakeApi {
  readonly blobs = new Map<string, Map<string, Stored>>()
  readonly keys = new Set<string>()
  readonly forgotten = new Set<string>()
  readonly forks = new Set<string>()
  /** Each request as `METHOD path`, and the content type it was sent with. */
  readonly seen: { method: string; path: string; contentType: string | null }[] = []
  /** Answer every request as a proxy would: no CRUD header. */
  proxy = false

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const path = new URL(request.url).pathname.slice(PREFIX.length)
    this.seen.push({ method: request.method, path, contentType: request.headers.get('content-type') })
    if (this.proxy) return new Response('bad gateway', { status: 502 })
    const answer = await this.#route(request, path)
    if (answer instanceof Response) return answer
    return new Response(JSON.stringify(answer.body), { status: answer.status ?? 200, headers: { 'x-scadbuddy-crud': '1' } })
  }

  async #route(request: Request, path: string): Promise<Response | { status?: number; body: unknown }> {
    const forget = /^\/forgotten-subjects\/(.+)$/.exec(path)?.[1]
    if (request.method === 'POST' && forget) {
      if (this.forks.has(forget)) return { status: 409, body: { detail: 'forks', reason: 'forks-take-ranges' } }
      this.forgotten.add(forget)
      this.blobs.delete(forget)
      return { body: { key: this.keys.delete(forget) } }
    }
    const drop = /^\/payload-keys\/(bridge-.+)\/drop$/.exec(path)?.[1]
    if (request.method === 'POST' && drop) return { body: { dropped: this.keys.delete(drop) } }
    if (request.method === 'POST' && path === '/payload-key-sweeps') return { body: { orphaned: 2, bridge: 1 } }
    const put = /^\/flow-blobs\/([^/]+)$/.exec(path)?.[1]
    if (request.method === 'POST' && put) {
      if (this.forgotten.has(put)) return { status: 410, body: { detail: 'forgotten', reason: 'subject-forgotten' } }
      const form = await request.formData()
      const metas = JSON.parse(String(form.get('images'))) as { name: string; media_type: string; size: number }[]
      const files = form.getAll('blob') as File[]
      const held = this.blobs.get(put) ?? new Map<string, Stored>()
      this.blobs.set(put, held)
      for (const [i, meta] of metas.entries()) {
        if (!held.has(meta.name)) held.set(meta.name, { mediaType: meta.media_type, size: meta.size, data: Buffer.from(await files[i]!.arrayBuffer()) })
      }
      return { body: { names: [...new Set(metas.map((m) => m.name))] } }
    }
    const got = /^\/flow-blobs\/([^/]+)\/([^/]+)$/.exec(path)
    if (request.method === 'GET' && got) {
      const found = this.blobs.get(got[1]!)?.get(got[2]!)
      if (!found) return { status: 404, body: { detail: 'none', reason: 'no-such-image' } }
      return new Response(new Uint8Array(found.data), {
        headers: { 'x-scadbuddy-crud': '1', 'content-type': 'application/octet-stream', 'x-scadbuddy-media-type': found.mediaType },
      })
    }
    return { status: 404, body: { detail: `no route ${request.method} ${path}` } }
  }
}

/** A `Sql` that fails the test when anything reaches it: the route did the work. */
const NO_SQL = new Proxy(() => undefined, {
  apply: () => {
    throw new Error('the SQL was used')
  },
  get: () => {
    throw new Error('the SQL was used')
  },
}) as unknown as Sql

describe('area forget', () => {
  it('is the one area of step 6', () => {
    expect([...FORGET_AREAS]).toEqual(['forget'])
  })
})

describe('CrudFlowBlobs', () => {
  let api: FakeApi
  let keys: MemoryKeys
  let blobs: CrudFlowBlobs
  beforeEach(() => {
    api = new FakeApi()
    keys = new MemoryKeys()
    blobs = new CrudFlowBlobs(BASE, keys, { fetch: api.fetch })
  })

  it('sends the images sealed, as bytes, and opens them again', async () => {
    const subject = `flow-${randomUUID()}`
    const png = sessionImage('image/png', PNG)
    expect(await blobs.put(subject, [png, png])).toEqual([png.name])
    const sent = api.seen[0]!
    expect(sent.method).toBe('POST')
    expect(sent.path).toBe(`/flow-blobs/${subject}`)
    // Multipart with the bytes in a part of their own: never JSON, never base64 (#5299 D8).
    expect(sent.contentType).toMatch(/^multipart\/form-data; boundary=/)
    const stored = api.blobs.get(subject)!.get(png.name)!
    expect(stored.mediaType).toBe('image/png')
    expect(stored.size).toBe(PNG.length)
    // Sealed under the run's key in the image's context: the API never sees the image.
    expect(stored.data.includes(PNG.subarray(0, 8))).toBe(false)
    expect(openBytes((await keys.keyFor(subject, false)), stored.data, flowBlobContext(subject, png.name))).toEqual(PNG)
    expect(await blobs.get(subject, png.name)).toEqual({ mediaType: 'image/png', bytes: PNG })
  })

  it('answers no image for one the run lacks or a malformed name, and asks nothing for the latter', async () => {
    const subject = `flow-${randomUUID()}`
    const name = sessionImage('image/png', PNG).name
    expect(await blobs.get(subject, name)).toBeUndefined()
    const asked = api.seen.length
    expect(await blobs.get(subject, '../x.png')).toBeUndefined()
    expect(await blobs.get(`session-${randomUUID()}`, name)).toBeUndefined()
    expect(api.seen.length).toBe(asked)
  })

  it('is SubjectForgotten for a forgotten run, and refuses a subject that is not one', async () => {
    const subject = `flow-${randomUUID()}`
    api.forgotten.add(subject)
    await expect(blobs.put(subject, [sessionImage('image/png', PNG)])).rejects.toBeInstanceOf(SubjectForgotten)
    // Its key is gone too: nothing is sent.
    keys.forgotten.add(subject)
    const asked = api.seen.length
    await expect(blobs.put(subject, [sessionImage('image/png', PNG)])).rejects.toBeInstanceOf(SubjectForgotten)
    expect(api.seen.length).toBe(asked)
    await expect(blobs.put(`session-${randomUUID()}`, [sessionImage('image/png', PNG)])).rejects.toThrow(/not a flow run/)
    expect(await blobs.put(subject, [])).toEqual([])
  })

  it("takes only the route's own answer", async () => {
    api.proxy = true
    const subject = `flow-${randomUUID()}`
    await expect(blobs.put(subject, [sessionImage('image/png', PNG)])).rejects.toThrow(/no CRUD route answered/)
    await expect(blobs.get(subject, sessionImage('image/png', PNG).name)).rejects.toThrow(/no CRUD route answered/)
  })

  it('goes through the routes once the area is listed, and through SQL until then', async () => {
    const calls: string[] = []
    const pg: FlowBlobStore = {
      put: async () => (calls.push('pg.put'), []),
      get: async () => (calls.push('pg.get'), undefined),
    }
    let listed = false
    const gated = gatedFlowBlobs(pg, blobs, () => listed)
    const subject = `flow-${randomUUID()}`
    const png = sessionImage('image/png', PNG)
    await gated.put(subject, [png])
    await gated.get(subject, png.name)
    expect(calls).toEqual(['pg.put', 'pg.get'])
    expect(api.seen).toEqual([])
    listed = true
    expect(await gated.put(subject, [png])).toEqual([png.name])
    expect(await gated.get(subject, png.name)).toEqual({ mediaType: 'image/png', bytes: PNG })
    expect(calls).toHaveLength(2)
  })
})

describe('CrudSubjectKeys', () => {
  it('forgets, drops and sweeps through the routes', async () => {
    const api = new FakeApi()
    const crud = new CrudSubjectKeys(BASE, { fetch: api.fetch })
    const flow = `flow-${randomUUID()}`
    const parent = `session-${randomUUID()}`
    const bridge = `bridge-${randomUUID()}`
    api.keys.add(flow)
    api.keys.add(bridge)
    api.forks.add(parent)
    expect(await crud.forget(flow)).toBe(true)
    expect(await crud.forget(flow)).toBe(false)
    expect(await crud.forget(parent)).toBe('forks')
    await crud.dropBridgeKey(bridge)
    expect(api.keys.has(bridge)).toBe(false)
    expect(await crud.sweep()).toBe(3)
    expect(api.seen.map((s) => `${s.method} ${s.path}`)).toEqual([
      `POST /forgotten-subjects/${flow}`,
      `POST /forgotten-subjects/${flow}`,
      `POST /forgotten-subjects/${parent}`,
      `POST /payload-keys/${bridge}/drop`,
      'POST /payload-key-sweeps',
    ])
    api.proxy = true
    await expect(crud.forget(flow)).rejects.toThrow(/no CRUD route answered/)
  })

  it('goes through the routes once the area is listed, and through SQL until then', async () => {
    const api = new FakeApi()
    const calls: string[] = []
    const pg: KeyDeletes = {
      dropBridgeKey: async () => void calls.push('pg.drop'),
      sweep: async () => (calls.push('pg.sweep'), 0),
    }
    let listed = false
    const deletes = gatedKeyDeletes(pg, new CrudSubjectKeys(BASE, { fetch: api.fetch }), () => listed)
    await deletes.dropBridgeKey(`bridge-${randomUUID()}`)
    expect(await deletes.sweep()).toBe(0)
    listed = true
    await deletes.dropBridgeKey(`bridge-${randomUUID()}`)
    expect(await deletes.sweep()).toBe(3)
    expect(calls).toEqual(['pg.drop', 'pg.sweep'])
    expect(api.seen).toHaveLength(2)
  })

  it("is forgetSubject's step 1 for a flow run, with no SQL of its own", async () => {
    const api = new FakeApi()
    const flow = `flow-${randomUUID()}`
    api.keys.add(flow)
    const dropped: string[] = []
    const done = await forgetSubject(
      { sql: NO_SQL, crud: new CrudSubjectKeys(BASE, { fetch: api.fetch }), keys: { forget: (s) => void dropped.push(s), keyFor: () => Promise.reject(new Error('unused')) } },
      flow,
    )
    expect(done).toEqual({ key: true, workflow: 'not_reached', rows: 0 })
    expect(api.forgotten.has(flow)).toBe(true)
    // This process's cached copy goes before and after, as with the SQL.
    expect(dropped).toEqual([flow, flow])
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`forgetSubject with the route${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('forgets a session with forks in its own transaction when the route refuses it', async () => {
    const kek = kekFromBase64(randomBytes(32).toString('base64'))
    const keys = new PgPayloadKeys(db.sql, kek)
    const subject = `session-${randomUUID()}`
    await keys.keyFor(subject, true)
    const asked: string[] = []
    const crud = { forget: async (s: string) => (asked.push(s), 'forks' as const) }
    const done = await forgetSubject({ sql: db.sql, keys, crud }, subject)
    expect(asked).toEqual([subject])
    expect(done.key).toBe(true)
    expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(0)
    expect(await db.sql`SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${subject}`).toHaveLength(1)
  })

  it('needs no KEK here for a session with forks once the route copies their ranges (#5299 step 9)', async () => {
    const parent = randomUUID()
    const fork = randomUUID()
    for (const [id, parentId, cut] of [
      [parent, null, null],
      [fork, parent, 2],
    ] as const) {
      await db.sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut)
        VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'idle', 10, 1, ${parentId}, ${cut})`
    }
    const asked: string[] = []
    // As the API does: the fork takes its range and its link goes, in the forget's transaction.
    const crud = {
      forget: async (s: string) => {
        asked.push(s)
        await db.sql`UPDATE ai_sessions SET fork_parent_id = NULL, fork_cut = NULL WHERE id = ${fork}`
        return true
      },
    }
    const done = await forgetSubject({ sql: db.sql, crud }, `session-${parent}`)
    expect(asked).toEqual([`session-${parent}`])
    expect(done.key).toBe(true)
  })
})
