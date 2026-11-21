import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { gated } from '../src/api/crudStores.js'
import { CrudReadError } from '../src/api/crudRead.js'
import { APPEND_ENTRIES_MAX, CrudTranscriptRows, OWN_MAX, appendBatches } from '../src/sessions/crudTranscripts.js'
import { PgTranscriptRows, PostgresSessionStore, type TranscriptRows } from '../src/sessions/store.js'
import { SubjectForgotten } from '../src/temporal/payloadCodec.js'

// A session's transcript through the API's CRUD routes (#5299 step 9, part b), against a fake API
// that answers as the backend's api/crud_agent_transcripts.py does (the backend's
// tests/crud/test_crud_agent_transcripts.py covers the routes, the sealing and the chain). An entry
// travels as its JSON text, a session id goes into a path only as a lowercase UUID, and the store
// keeps its interface: each method goes to its route once the area is listed, its SQL until then.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const KEY = { projectKey: 'p', sessionId: SESSION }

type Seen = { method: string; path: string; body: unknown }

class FakeApi {
  seen: Seen[] = []
  answers = new Map<string, { status: number; body: unknown }>()
  /** Answers each POST of entries in turn (a batch's `written`), then `written: true`. */
  written: boolean[] = []

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = `${url.pathname.slice(PREFIX.length)}${url.search}`
    const text = request.method === 'POST' ? await request.text() : ''
    this.seen.push({ method: request.method, path, body: text ? JSON.parse(text) : undefined })
    const key = `${request.method} ${path}`
    if (request.method === 'POST' && path === `/sessions/${SESSION}/entries` && !this.answers.has(key)) {
      return new Response(JSON.stringify({ written: this.written.shift() ?? true }), { headers: { 'x-scadbuddy-crud': '1' } })
    }
    const answer = this.answers.get(key) ?? { status: 404, body: { detail: `no answer for ${key}` } }
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
  }

  answer(key: string, body: unknown, status = 200): this {
    this.answers.set(key, { status, body })
    return this
  }
}

const noSql = new Proxy(() => {}, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

const rows = (api: FakeApi) => new CrudTranscriptRows(BASE, { fetch: api.fetch })

describe('a transcript over the CRUD routes', () => {
  it('appends the entries as their JSON text, with the fence, and loads them deep-equal', async () => {
    const entries = [
      { type: 'user', uuid: 'u1', message: 'nul \u0000, é and 🙂' },
      { type: 'last-prompt', text: 'no uuid' },
    ] as unknown as SessionStoreEntry[]
    const api = new FakeApi().answer(`GET /sessions/${SESSION}/transcript`, { entries: entries.map((e) => JSON.stringify(e)) })
    const store = rows(api)
    expect(await store.append({ ...KEY, subpath: 'subagents/a' }, entries, { attempt: 'run:1', turnId: 'turn-1' })).toBe(true)
    expect(api.seen[0]).toEqual({
      method: 'POST',
      path: `/sessions/${SESSION}/entries`,
      body: {
        project_key: 'p',
        subpath: 'subagents/a',
        entries: entries.map((e) => JSON.stringify(e)),
        fence: { attempt: 'run:1', turn_id: 'turn-1' },
      },
    })
    await store.append(KEY, entries)
    expect((api.seen[1]?.body as Record<string, unknown>).fence).toBeUndefined()
    expect((api.seen[1]?.body as Record<string, unknown>).subpath).toBe('')
    expect(await store.transcript(SESSION)).toEqual(entries)
  })

  it('sends a long append in batches in order, and stops at the first one the fence refuses', async () => {
    const entries = Array.from({ length: APPEND_ENTRIES_MAX * 2 + 1 }, (_, i) => ({ type: 'x', uuid: `u${i}` })) as unknown as SessionStoreEntry[]
    const api = new FakeApi()
    expect(await rows(api).append(KEY, entries)).toBe(true)
    expect(api.seen.map((s) => (s.body as { entries: string[] }).entries.length)).toEqual([APPEND_ENTRIES_MAX, APPEND_ENTRIES_MAX, 1])
    expect(api.seen.flatMap((s) => (s.body as { entries: string[] }).entries)).toEqual(entries.map((e) => JSON.stringify(e)))

    const refused = new FakeApi()
    refused.written = [true, false]
    expect(await rows(refused).append(KEY, entries, { attempt: 'a', turnId: 't' })).toBe(false)
    expect(refused.seen).toHaveLength(2)
  })

  it('batches by the bytes the request carries too, never splitting an entry', () => {
    const big = 'x'.repeat(12 * 1024 * 1024)
    const batches = appendBatches([big, big, big, 'small'])
    expect(batches.map((b) => b.length)).toEqual([2, 2])
    // An escaped character is counted as the JSON carries it (six bytes for a control character).
    const escaped = '\u0001'.repeat(6 * 1024 * 1024)
    expect(appendBatches([escaped, escaped]).map((b) => b.length)).toEqual([1, 1])
  })

  it('tells a forgotten session by its 410, and any other failure as it came', async () => {
    const api = new FakeApi()
      .answer(`POST /sessions/${SESSION}/entries`, { detail: 'the session was forgotten', reason: 'forgotten' }, 410)
      .answer(`GET /sessions/${SESSION}/transcript`, { detail: 'no key', reason: 'unavailable' }, 503)
    await expect(rows(api).append(KEY, [{ type: 'x' }] as unknown as SessionStoreEntry[])).rejects.toBeInstanceOf(SubjectForgotten)
    const failure = await rows(api)
      .transcript(SESSION)
      .catch((e: unknown) => e)
    expect(failure).toBeInstanceOf(CrudReadError)
    expect((failure as CrudReadError).status).toBe(503)
  })

  it('reads nothing for an id that is no UUID, and never writes to one', async () => {
    const api = new FakeApi()
    const store = rows(api)
    for (const bad of ['not-a-uuid', '../areas', `${SESSION}/../../areas`, '', `${SESSION}?x=1`]) {
      expect(await store.transcript(bad)).toEqual([])
      expect(await store.subpath(bad, 'subagents/a')).toEqual([])
      expect(await store.lastOfType(bad, '', 'cost-state')).toBeNull()
      expect(await store.lastCwd(bad)).toBeUndefined()
      expect(await store.listSubkeys(bad)).toEqual([])
      expect(await store.head(bad)).toEqual({ exists: false, since: null })
      expect(await store.userLanded(bad, 0)).toBe(false)
      await expect(store.append({ ...KEY, sessionId: bad }, [{ type: 'x' }] as unknown as SessionStoreEntry[])).rejects.toThrow('is not a session id')
      await expect(store.delete(bad)).rejects.toThrow('is not a session id')
      await expect(store.moved(bad, null, [])).rejects.toThrow('is not a session id')
    }
    expect(api.seen).toEqual([])
  })

  it('names an uppercase id lowercase, and puts a subpath and a type only in the query', async () => {
    const api = new FakeApi()
      .answer(`GET /sessions/${SESSION}/entries/last?type=cost-state&subpath=..%2Fx`, { entry: '{"type":"cost-state","usd":2}' })
      .answer(`GET /sessions/${SESSION}/entries?subpath=..%2F..%2Fareas`, { entries: null })
      .answer(`GET /transcripts?project_key=%2Fp%2F..`, { sessions: [{ session_id: SESSION, mtime: 5 }] })
    const store = rows(api)
    expect(await store.lastOfType(SESSION.toUpperCase(), '../x', 'cost-state')).toEqual({ type: 'cost-state', usd: 2 })
    expect(await store.subpath(SESSION, '../../areas')).toEqual([])
    expect(await store.listSessions('/p/..')).toEqual([{ sessionId: SESSION, mtime: 5 }])
  })

  it('answers the other reads from their routes', async () => {
    const api = new FakeApi()
      .answer(`GET /sessions/${SESSION}/cwd`, { cwd: '/w/a' })
      .answer(`GET /sessions/${SESSION}/subkeys`, { subkeys: ['subagents/a'] })
      .answer(`GET /sessions/${SESSION}/transcript/head`, { exists: true, since: '41' })
      .answer(`POST /sessions/${SESSION}/transcript/moved`, { moved: true })
      .answer(`GET /sessions/${SESSION}/entries/landed?after=41`, { landed: true })
      .answer(`POST /sessions/${SESSION}/entries/delete`, { deleted: true })
    const store = rows(api)
    expect(await store.lastCwd(SESSION)).toBe('/w/a')
    expect(await store.listSubkeys(SESSION)).toEqual(['subagents/a'])
    expect(await store.head(SESSION)).toEqual({ exists: true, since: '41' })
    expect(await store.moved(SESSION, '41', ['u1'])).toBe(true)
    expect(api.seen.at(-1)?.body).toEqual({ since: '41', own: ['u1'] })
    expect(await store.userLanded(SESSION, 41)).toBe(true)
    await store.delete(SESSION, 'subagents/a')
    expect(api.seen.at(-1)?.body).toEqual({ subpath: 'subagents/a' })
    await store.delete(SESSION)
    expect(api.seen.at(-1)?.body).toEqual({ subpath: null })
  })

  it('sends a subpath and a project key well-formed', async () => {
    const api = new FakeApi()
    await rows(api).append({ projectKey: 'p\ud800', sessionId: SESSION, subpath: 's\udc00' }, [{ type: 'x' }] as unknown as SessionStoreEntry[])
    expect(api.seen[0]?.body).toMatchObject({ project_key: 'p\ufffd', subpath: 's\ufffd' })
  })

  it('counts a tail past the uuids the route takes as moved, asking nothing', async () => {
    const api = new FakeApi()
    expect(await rows(api).moved(SESSION, '1', Array.from({ length: OWN_MAX + 1 }, (_, i) => `u${i}`))).toBe(true)
    expect(api.seen).toEqual([])
  })

  it('keeps the store on its SQL until the area is listed, then on the routes, in append order', async () => {
    const api = new FakeApi().answer(`GET /sessions/${SESSION}/transcript`, { entries: ['{"type":"x"}'] })
    const listed = { on: false }
    const sql = new PgTranscriptRows(noSql, undefined)
    const chosen = gated<TranscriptRows>(sql, rows(api), () => listed.on)
    const store = new PostgresSessionStore(noSql, { rows: chosen })
    await expect(store.load(KEY)).rejects.toThrow('ran SQL')
    listed.on = true
    expect(await store.load(KEY)).toEqual([{ type: 'x' }])
    await Promise.all([
      store.append(KEY, [{ type: 'a' }] as unknown as SessionStoreEntry[]),
      store.append(KEY, [{ type: 'b' }] as unknown as SessionStoreEntry[]),
    ])
    const posted = api.seen.filter((s) => s.method === 'POST').map((s) => (s.body as { entries: string[] }).entries)
    expect(posted).toEqual([['{"type":"a"}'], ['{"type":"b"}']])
  })
})
