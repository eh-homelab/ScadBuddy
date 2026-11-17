import { describe, expect, it, vi } from 'vitest'
import { CrudEditRows } from '../src/sessions/edits.js'
import { SessionError } from '../src/sessions/manager.js'
import { ROW_WRITE_TIMEOUT_MS } from '../src/sessions/turnRows.js'

// An edit of a session through the API's CRUD route (#6449), against a fake API that answers as
// the backend's api/crud_agent_edits.py does (tests/crud/test_crud_agent_edits.py covers the
// route; test/editRowsParity.pg.test.ts the port against the SQL). The edit is sent untold first;
// the workflow is told it is done only when the route answers `untold` (every other check passed,
// nothing written), and the edit is then sent again, told. A refusal or a 503 tells no workflow, a
// failed telling writes nothing, and each refusal is the SessionError the SQL path throws.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const BROWSER = { kind: 'browser' as const, id: 'browser' }

type Seen = { path: string; body: Record<string, unknown> }

/** Answers each call with the next of `answers`, the last one again once they run out. */
function fakeApi(...answers: [number, unknown][]) {
  const seen: Seen[] = []
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const request = new Request(input, init)
    seen.push({ path: new URL(request.url).pathname.slice(PREFIX.length), body: JSON.parse(await request.text()) as Record<string, unknown> })
    const [status, body] = answers[Math.min(seen.length, answers.length) - 1]!
    return new Response(JSON.stringify(body), { status, headers: { 'x-scadbuddy-crud': '1' } })
  }
  return { seen, rows: new CrudEditRows(BASE, { fetch }) }
}

const ok = { logged: [], dismissed: [] }

describe('an edit over the CRUD route', () => {
  it('tells the workflow only once the route answers untold, then writes the done as told', async () => {
    const untold: [number, unknown] = [409, { detail: 'refused', reason: 'untold' }]
    const { seen, rows } = fakeApi(untold, [
      200,
      { logged: [{ session_id: SESSION, events: [{ v: 1, type: 'session.status', sessionId: SESSION, status: 'done' }], seqs: [3] }], dismissed: [] },
    ])
    const end = vi.fn(async () => {})
    const edited = await rows.edit(SESSION.toUpperCase(), { principal: BROWSER, done: true }, end)
    expect(end).toHaveBeenCalledWith(SESSION.toUpperCase())
    const body = { principal: BROWSER, title: null, done: true, archived: null }
    expect(seen).toEqual([
      { path: `/sessions/${SESSION}/edit`, body: { ...body, told: false } },
      { path: `/sessions/${SESSION}/edit`, body: { ...body, told: true } },
    ])
    expect(edited.logged).toEqual({ events: [{ v: 1, type: 'session.status', sessionId: SESSION, status: 'done' }], seqs: [3] })
  })

  it('tells no workflow when the route takes the edit untold, refuses it or does not answer', async () => {
    for (const answer of [
      [200, ok],
      [409, { detail: 'refused', reason: 'parked' }],
      [409, { detail: 'refused', reason: 'busy_done' }],
      [503, { detail: 'busy', reason: 'row_busy' }],
    ] as [number, unknown][]) {
      const { seen, rows } = fakeApi(answer)
      const end = vi.fn(async () => {})
      await rows.edit(SESSION, { principal: BROWSER, done: true, archived: true }, end).catch(() => {})
      expect(end, JSON.stringify(answer)).not.toHaveBeenCalled()
      expect(seen.map((s) => s.body.told)).toEqual([false])
    }
    const lost: typeof globalThis.fetch = async () => Promise.reject(new TypeError('fetch failed'))
    const end = vi.fn(async () => {})
    await expect(new CrudEditRows(BASE, { fetch: lost }).edit(SESSION, { principal: BROWSER, done: true }, end)).rejects.toThrow()
    expect(end).not.toHaveBeenCalled()
  })

  it('tries a told send once more when its answer was lost or the route was busy', async () => {
    const untold: [number, unknown] = [409, { detail: 'refused', reason: 'untold' }]
    const { seen, rows } = fakeApi(untold, [503, { detail: 'busy', reason: 'row_busy' }], [200, ok])
    const end = vi.fn(async () => {})
    await rows.edit(SESSION, { principal: BROWSER, done: true }, end)
    expect(end).toHaveBeenCalledTimes(1)
    expect(seen.map((s) => s.body.told)).toEqual([false, true, true])
    // A refusal of the told send is not tried again.
    const refused = fakeApi(untold, [409, { detail: 'refused', reason: 'busy_done' }], [200, ok])
    await expect(refused.rows.edit(SESSION, { principal: BROWSER, done: true }, async () => {})).rejects.toMatchObject({ code: 'busy' })
    expect(refused.seen.map((s) => s.body.told)).toEqual([false, true])
  })

  it('writes nothing when the workflow cannot be told', async () => {
    const { seen, rows } = fakeApi([409, { detail: 'refused', reason: 'untold' }], [200, ok])
    const end = async () => {
      throw new SessionError('unavailable', 'no Temporal')
    }
    await expect(rows.edit(SESSION, { principal: BROWSER, done: true }, end)).rejects.toThrow(/no Temporal/)
    expect(seen.map((s) => s.body.told)).toEqual([false])
  })

  it('hears each refusal as the SQL path throws it', async () => {
    const cases: [number, string, string, RegExp][] = [
      [404, 'gone', 'not_found', /no session/],
      [409, 'changed_owner', 'busy', /changed owner meanwhile/],
      [409, 'busy_done', 'busy', /Stop it first, then mark it done/],
      [409, 'busy_archive', 'busy', /Stop it first, then archive it/],
      [409, 'parked', 'busy', /waiting for you/],
    ]
    for (const [status, reason, code, message] of cases) {
      const { rows } = fakeApi([status, { detail: 'refused', reason }])
      const refused = await rows.edit(SESSION, { principal: BROWSER, title: 'x', done: false }, async () => {}).catch((e: unknown) => e)
      expect(refused, reason).toBeInstanceOf(SessionError)
      expect((refused as SessionError).code, reason).toBe(code)
      expect((refused as SessionError).message, reason).toMatch(message)
    }
    // Anything else is no refusal the edit knows: it throws as it came.
    const { rows } = fakeApi([503, { detail: 'down' }])
    await expect(rows.edit(SESSION, { principal: BROWSER, title: 'x', done: false }, async () => {})).rejects.not.toBeInstanceOf(SessionError)
  })

  it('waits for the route past its own waits, as the turn routes do', async () => {
    expect(new CrudEditRows(BASE).timeoutMs).toBe(ROW_WRITE_TIMEOUT_MS)
    // The route waits up to 10 s for a connection, then up to 10 s for the row's lock.
    expect(ROW_WRITE_TIMEOUT_MS).toBeGreaterThan(20_000)
    // Each call's request is bounded by it, the told one too.
    const timeout = vi.spyOn(AbortSignal, 'timeout')
    try {
      const { rows } = fakeApi([409, { detail: 'refused', reason: 'untold' }], [200, ok])
      await rows.edit(SESSION, { principal: BROWSER, done: true }, async () => {})
      expect(timeout.mock.calls).toEqual([[ROW_WRITE_TIMEOUT_MS], [ROW_WRITE_TIMEOUT_MS]])
    } finally {
      timeout.mockRestore()
    }
  })

  it('sends no call for an id that is no session', async () => {
    const { seen, rows } = fakeApi([200, ok])
    for (const bad of ['../x', `${SESSION}/../../stores`, '..', 'not-a-uuid']) {
      const refused = await rows.edit(bad, { principal: BROWSER, title: 'x', done: false }, async () => {}).catch((e: unknown) => e)
      expect((refused as SessionError).code).toBe('not_found')
    }
    expect(seen).toEqual([])
  })
})
