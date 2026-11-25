import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { TOUCHES_NOTHING } from '../src/sessions/touched.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #7273 G4: an agent reads and writes a tag through the phone page's NFC jobs.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const JOB = '8d6f3c1e-0000-4000-8000-000000007273'
const JOBS = `${BACKEND}/api/v1/filament/nfc-jobs`
const REC = { record_type: 'mime', media_type: 'application/json', data_hex: '7b7d' }

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(signal: AbortSignal = new AbortController().signal): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal,
  }
}

/** A job whose server-side deadline is `inMs` from now (#7273 I1: the tool waits for it). */
function job(state: string, extra: Record<string, unknown> = {}, inMs = 600_000) {
  const expires_at = new Date(Date.now() + inMs).toISOString()
  return {
    id: JOB,
    kind: 'read',
    records: null,
    state,
    result: null,
    created_by: 'test',
    armed_by: null,
    expires_at,
    ...extra,
  }
}

describe('NFC job tools', () => {
  it.each([
    ['nfc_read', 'write', false],
    ['nfc_write', 'outward', false],
  ])('%s is %s tier, gated: %s', (name, risk, gated) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(gated)
    expect(TOUCHES_NOTHING.has(name)).toBe(true)
    expect(tool(name).source).toMatch(/physical tag/)
  })

  it('nfc_read creates a read job and answers its result once done', async () => {
    const bodies: unknown[] = []
    let reads = 0
    const result = { next: 'bind_or_create', uid: '04AA', records: [REC] }
    server.use(
      http.post(JOBS, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(job('pending'), { status: 201 })
      }),
      http.get(`${JOBS}/${JOB}`, () => {
        reads += 1
        return HttpResponse.json(reads < 3 ? job(reads === 1 ? 'pending' : 'armed') : job('done', { result }))
      }),
    )
    const got = await runTool(tool('nfc_read'), { timeout_s: 30 }, ctx())
    expect(got.isError).toBeFalsy()
    expect(bodies).toEqual([{ kind: 'read', ttl_s: 30 }])
    expect(firstText(got)).toEqual({ job_id: JOB, state: 'done', result })
    expect(reads).toBe(3)
  })

  it('nfc_write sends the records and answers expired when the job expires', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(JOBS, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(job('pending', { kind: 'write', records: [REC] }), { status: 201 })
      }),
      http.get(`${JOBS}/${JOB}`, () => HttpResponse.json(job('expired', { kind: 'write' }))),
    )
    const got = await runTool(tool('nfc_write'), { records: [REC] }, ctx())
    expect(got.isError).toBeFalsy()
    expect(bodies).toEqual([{ kind: 'write', records: [REC], ttl_s: 600 }])
    expect(firstText(got)).toEqual({ job_id: JOB, state: 'expired', result: null })
  })

  it('nfc_write refuses records past 16', async () => {
    let posted = false
    server.use(
      http.post(JOBS, () => {
        posted = true
        return HttpResponse.json(job('pending'), { status: 201 })
      }),
    )
    const got = await runTool(tool('nfc_write'), { records: Array(17).fill(REC) }, ctx())
    expect(got.isError).toBe(true)
    expect(posted).toBe(false)
    const none = await runTool(tool('nfc_write'), { records: [] }, ctx())
    expect(none.isError).toBe(true)
  })

  it("waits until the server's expires_at, not its own timeout, and answers the job's real state", async () => {
    // #7273 I1: the job is made after the call starts, so it ends later than timeout_s from then.
    const started = Date.now()
    const result = { next: 'bind_or_create', uid: '04AA', records: [] }
    let cancels = 0
    server.use(
      http.post(JOBS, () => HttpResponse.json(job('pending', {}, 1_500), { status: 201 })),
      http.get(`${JOBS}/${JOB}`, () =>
        HttpResponse.json(Date.now() - started < 1_200 ? job('armed') : job('done', { result })),
      ),
      http.post(`${JOBS}/${JOB}/cancel`, () => {
        cancels += 1
        return HttpResponse.json(job('cancelled'))
      }),
    )
    const got = await runTool(tool('nfc_read'), { timeout_s: 1 }, { ...ctx(), pollIntervalMs: 50 })
    expect(firstText(got)).toEqual({ job_id: JOB, state: 'done', result })
    expect(cancels).toBe(0)
  })

  it('cancels a job still open past its expires_at and answers what the server then says', async () => {
    // #7273 M1: no page may arm the job once the agent stopped waiting for it.
    let cancelled = false
    const expires_at = new Date(Date.now() + 100).toISOString()
    server.use(
      http.post(JOBS, () => HttpResponse.json(job('pending', { expires_at }), { status: 201 })),
      http.get(`${JOBS}/${JOB}`, () => HttpResponse.json(job(cancelled ? 'cancelled' : 'pending', { expires_at }))),
      http.post(`${JOBS}/${JOB}/cancel`, () => {
        cancelled = true
        return HttpResponse.json(job('cancelled'))
      }),
    )
    const got = await runTool(tool('nfc_read'), { timeout_s: 1 }, { ...ctx(), pollIntervalMs: 20 })
    expect(cancelled).toBe(true)
    expect(firstText(got)).toEqual({ job_id: JOB, state: 'cancelled', result: null })
  })

  it('answers the done result of a write that finished after the first expires_at', async () => {
    // #7419 review: arming moves expires_at, and a write that began is not expired at it.
    const made = Date.now()
    const first = new Date(made + 100).toISOString()
    const armed = new Date(made + 300).toISOString()
    const result = { uid: '04AA', records: [REC], backup_id: 'b' }
    let cancels = 0
    server.use(
      http.post(JOBS, () => HttpResponse.json(job('pending', { kind: 'write', records: [REC], expires_at: first }), { status: 201 })),
      http.get(`${JOBS}/${JOB}`, () =>
        HttpResponse.json(Date.now() - made < 700 ? job('armed', { kind: 'write', expires_at: armed }) : job('done', { kind: 'write', expires_at: armed, result })),
      ),
      http.post(`${JOBS}/${JOB}/cancel`, () => {
        cancels += 1
        return HttpResponse.json(job('cancelled'))
      }),
    )
    const got = await runTool(tool('nfc_write'), { records: [REC], timeout_s: 1 }, { ...ctx(), pollIntervalMs: 50 })
    expect(firstText(got)).toEqual({ job_id: JOB, state: 'done', result })
    expect(cancels).toBe(0)
  })

  it('cancels the job when the call is aborted', async () => {
    let cancelled = false
    server.use(
      http.post(JOBS, () => HttpResponse.json(job('pending'), { status: 201 })),
      http.post(`${JOBS}/${JOB}/cancel`, () => {
        cancelled = true
        return HttpResponse.json(job('cancelled'))
      }),
      http.get(`${JOBS}/${JOB}`, () => HttpResponse.json(job(cancelled ? 'cancelled' : 'pending'))),
    )
    const abort = new AbortController()
    setTimeout(() => abort.abort(), 30)
    const started = Date.now()
    const got = await runTool(tool('nfc_read'), { timeout_s: 600 }, { ...ctx(abort.signal), pollIntervalMs: 1000 })
    expect(Date.now() - started).toBeLessThan(900)
    expect(cancelled).toBe(true)
    expect(firstText(got)).toEqual({ job_id: JOB, state: 'cancelled', result: null })
  })

  it('still answers when the cancel on abort fails, with the last state it read and why', async () => {
    server.use(
      http.post(JOBS, () => HttpResponse.json(job('pending'), { status: 201 })),
      http.post(`${JOBS}/${JOB}/cancel`, () => HttpResponse.json({ title: 'Internal Server Error', status: 500, detail: 'down' }, { status: 500 })),
      http.get(`${JOBS}/${JOB}`, () => HttpResponse.json(job('pending'))),
    )
    const abort = new AbortController()
    setTimeout(() => abort.abort(), 30)
    const got = await runTool(tool('nfc_read'), { timeout_s: 600 }, { ...ctx(abort.signal), pollIntervalMs: 1000 })
    expect(got.isError).toBeFalsy()
    expect(firstText(got)).toEqual({ job_id: JOB, state: 'pending', result: null, cancel_failed: expect.stringContaining('HTTP 500') })
  })

  it('names the cancel route among its routes', () => {
    for (const name of ['nfc_read', 'nfc_write']) {
      expect(tool(name).routes).toContain('POST /api/v1/filament/nfc-jobs/{job_id}/cancel')
    }
  })
})
