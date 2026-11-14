import { createHash, randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { EventLog } from '../src/sessions/eventLog.js'
import { event, type Owner, type ServerEvent } from '../src/sessions/protocol.js'
import { copyForkRanges } from '../src/sessions/store.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// A fork's event log and images are copy-on-write too (#4212): the child reads its parent's
// conversation events through its event cut, and its parent's images by name, and forgetting the
// parent copies the child's range into the child first.

const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const IMAGE = `${createHash('sha256').update(PNG).digest('hex')}.png`

describe.skipIf(!TEST_DATABASE_URL)(`fork event logs and images${TEST_DATABASE_URL ? '' : ` (set ${TEST_DATABASE_URL_ENV})`}`, () => {
  let t: Throwaway
  let log: EventLog
  let blobs: SessionBlobs

  beforeEach(async () => {
    t = await throwawayDatabase()
    expect(await t.db.ready()).toBe(true)
    log = new EventLog(t.db.sql)
    blobs = new SessionBlobs(t.db.sql)
  })
  afterEach(async () => {
    await t?.drop()
  })

  /** A session row, and its `session.started` as its seq 1, as every session's log begins. */
  async function session(parent?: string): Promise<string> {
    const id = randomUUID()
    await t.db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd,
                               fork_parent_id, fork_cut)
      VALUES (${id}, 'mcp', 'browser', 'browser', 'Someone', 'browser', 'browser', 'idle', 10, 1,
              ${parent ?? null}, ${parent ? 0 : null})`
    await log.append(id, [event({ type: 'session.started', sessionId: id, origin: 'mcp', owner: BROWSER, title: 't' })])
    return id
  }

  /** A fork of `parent` that takes its log through seq `cut`, as SessionManager.fork makes one. */
  async function fork(parent: string, cut: number): Promise<string> {
    const child = await session(parent)
    await log.inheritThrough(child, cut)
    await log.append(child, [event({ type: 'session.status', sessionId: child, status: 'idle' })])
    return child
  }

  function turn(sessionId: string, name: string): ServerEvent[] {
    return [
      event({ type: 'user.turn', sessionId, turnId: name, text: `turn:${name}`, author: BROWSER }),
      event({ type: 'session.status', sessionId, status: 'running' }),
      event({ type: 'assistant.text.delta', sessionId, messageId: `m-${name}`, delta: `reply to ${name}` }),
      event({ type: 'assistant.text.done', sessionId, messageId: `m-${name}` }),
      event({ type: 'session.status', sessionId, status: 'idle' }),
    ]
  }

  function imageResult(sessionId: string, id: string, name: string): ServerEvent {
    return event({ type: 'tool.result', sessionId, id, ok: true, summary: 'ok', images: [{ name, mediaType: 'image/png' }] })
  }

  async function all(sessionId: string, page = 500): Promise<{ seq: number; event: ServerEvent }[]> {
    const out: { seq: number; event: ServerEvent }[] = []
    for (let after = 0; ; ) {
      const rows = await log.read(sessionId, after, page)
      if (rows.length === 0) return out
      out.push(...rows)
      after = rows.at(-1)!.seq
    }
  }

  /** How many rows `table` holds for the session: what it holds itself, not what it reads. */
  async function rowsOf(table: 'ai_session_events' | 'ai_session_blobs', sessionId: string): Promise<number> {
    const [row] = await t.db.sql<{ n: string }[]>`SELECT count(*)::text AS n FROM ${t.db.sql(table)} WHERE session_id = ${sessionId}`
    return Number(row?.n)
  }

  const texts = (rows: { event: ServerEvent }[]) =>
    rows.map(({ event: e }) => (e.type === 'user.turn' ? e.text : e.type === 'assistant.text.delta' ? e.delta : e.type))

  it("reads its own start, then its parent's conversation through the cut as its own, then its own events", async () => {
    const parent = await session()
    const [, , , done] = await log.append(parent, turn(parent, 'a'))
    await log.append(parent, turn(parent, 'b'))
    const child = await fork(parent, done!)
    await log.append(child, turn(child, 'c'))

    const rows = await all(child)
    expect(texts(rows)).toEqual([
      'session.started',
      'turn:a',
      'reply to a',
      'assistant.text.done',
      'session.status',
      'turn:c',
      'session.status',
      'reply to c',
      'assistant.text.done',
      'session.status',
    ])
    expect(rows.every(({ event: e }) => 'sessionId' in e && e.sessionId === child)).toBe(true)
    // In order, and the parent's seqs up to the cut, so a reader resumes by seq across the cut.
    expect(rows.map((r) => r.seq)).toEqual([...rows.map((r) => r.seq)].sort((a, b) => a - b))
    expect(rows.map((r) => r.seq).slice(0, 4)).toEqual([1, 2, 4, 5])
    expect(await log.lastSeq(child)).toBe(rows.at(-1)!.seq)
    // Nothing was copied: the child holds only its own rows.
    expect(await rowsOf('ai_session_events', child)).toBe(7)
    // A page boundary inside the inherited range.
    expect(texts(await all(child, 2))).toEqual(texts(rows))
  })

  it('a fork of a fork reads both cuts', async () => {
    const a = await session()
    const [, , , doneA] = await log.append(a, turn(a, 'a1'))
    await log.append(a, turn(a, 'a2'))
    const b = await fork(a, doneA!)
    const [, , , doneB] = await log.append(b, turn(b, 'b1'))
    await log.append(b, turn(b, 'b2'))
    const c = await fork(b, doneB!)

    expect(texts(await all(c)).filter((x) => x.startsWith('turn:') || x.startsWith('reply'))).toEqual([
      'turn:a1',
      'reply to a1',
      'turn:b1',
      'reply to b1',
    ])
    expect((await all(c)).every(({ event: e }) => 'sessionId' in e && e.sessionId === c)).toBe(true)
  })

  it('a fork made before the event cut reads only its own copied rows', async () => {
    const parent = await session()
    await log.append(parent, turn(parent, 'a'))
    const child = await session(parent)
    expect(texts(await all(child))).toEqual(['session.started'])
  })

  it("serves the parent's images that the events it takes name, and none past its cut", async () => {
    const parent = await session()
    const [shown] = await log.append(parent, [imageResult(parent, 'toolu_1', IMAGE)])
    const LATER = `${'0'.repeat(64)}.png`
    await log.append(parent, [imageResult(parent, 'toolu_2', LATER)])
    for (const name of [IMAGE, LATER]) {
      await t.db.sql`INSERT INTO ai_session_blobs (session_id, name, media_type, data) VALUES (${parent}, ${name}, 'image/png', ${PNG})`
    }
    const child = await fork(parent, shown!)
    expect((await blobs.get(child, IMAGE))?.bytes).toEqual(PNG)
    expect(await blobs.get(child, LATER)).toBeUndefined()
    expect(await rowsOf('ai_session_blobs', child)).toBe(0)
    // A legacy fork (no event cut) does not read through.
    const legacy = await session(parent)
    expect(await blobs.get(legacy, IMAGE)).toBeUndefined()
  })

  it("forgetting the parent's rows copies the child's range and the images it names into the child", async () => {
    const parent = await session()
    const [, , , done] = await log.append(parent, [
      ...turn(parent, 'a').slice(0, 3),
      event({ type: 'tool.call', sessionId: parent, id: 'toolu_1', name: 'x', input: {}, risk: 'read' }),
    ])
    await log.append(parent, [event({ type: 'tool.result', sessionId: parent, id: 'toolu_1', ok: true, summary: 'ok', images: [{ name: IMAGE, mediaType: 'image/png' }] })])
    await t.db.sql`INSERT INTO ai_session_blobs (session_id, name, media_type, data) VALUES (${parent}, ${IMAGE}, 'image/png', ${PNG})`
    const cut = (await log.lastSeq(parent))
    expect(done).toBeLessThan(cut)
    const child = await fork(parent, cut)
    await log.append(child, turn(child, 'c'))
    const before = await all(child)

    await t.db.sql.begin(async (tx) => {
      expect(await copyForkRanges(tx, testPayloadKeys(t.db.sql), parent)).toBe(1)
    })
    const [link] = await t.db.sql<{ parent: string | null; cut: string | null }[]>`
      SELECT fork_parent_id::text AS parent, fork_event_cut::text AS cut FROM ai_sessions WHERE id = ${child}`
    expect(link).toEqual({ parent: null, cut: null })
    await t.db.sql`DELETE FROM ai_session_events WHERE session_id = ${parent}`
    await t.db.sql`DELETE FROM ai_session_blobs WHERE session_id = ${parent}`

    expect(await all(child)).toEqual(before)
    expect((await blobs.get(child, IMAGE))?.bytes).toEqual(PNG)
  })
})
