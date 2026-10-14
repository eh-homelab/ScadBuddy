import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import type { RunQueue } from '../src/flows/queue.js'
import { originPolicy } from '../src/http/origins.js'
import { registerFlowQueueRoutes } from '../src/routes/flowQueue.js'

// GET /api/v1/ai/flow-runs/{id}/queue (#2482, spec 2026-10-11 §4.5): the person's read of
// a run's one queue, behind the UI read guard like the pending-input reads.

const UI = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https' }
const QUEUE: RunQueue = { run_id: 'r1', name: 'swap', status: 'waiting', starter_session: null, entries: [] }

function setup(options: { database?: boolean; ready?: boolean } = {}) {
  const app = new Hono()
  registerFlowQueueRoutes(app, {
    queue: options.database === false ? undefined : { forRun: (id) => Promise.resolve(id === 'r1' ? QUEUE : undefined) },
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return app
}

const get = (app: Hono, id: string, headers: Record<string, string> = UI) =>
  app.request(`https://scadbuddy.example/api/v1/ai/flow-runs/${id}/queue`, { headers })

describe('the flow run queue route', () => {
  it("answers a run's queue, and 404 for a run there is not", async () => {
    const app = setup()
    const ok = await get(app, 'r1')
    expect(ok.status).toBe(200)
    expect(await ok.json()).toEqual(QUEUE)
    expect((await get(app, 'nope')).status).toBe(404)
  })

  it('refuses a cross-site read, and answers 503 without a ready database', async () => {
    expect((await get(setup(), 'r1', { ...UI, origin: 'https://evil.example', 'sec-fetch-site': 'cross-site' })).status).toBe(403)
    expect((await get(setup({ database: false }), 'r1')).status).toBe(503)
    expect((await get(setup({ ready: false }), 'r1')).status).toBe(503)
  })
})
