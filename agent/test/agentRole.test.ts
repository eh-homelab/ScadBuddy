import { describe, expect, it } from 'vitest'
import { queuesPolled, workerHealthApp, type WorkerHealth } from '../src/runtime/health.js'
import { roleParts } from '../src/runtime/role.js'

// SCADBUDDY_AGENT_ROLE (spec 2026-10-13 §14.3, plan Step 6.1): one image, run as the API's HTTP
// half, as the worker that runs turns, or as both. main.ts starts what `roleParts` names.

describe('what each role starts', () => {
  it('all: everything, as before the roles', () => {
    expect(roleParts('all')).toEqual({ http: true, sharedWorkers: true, sessionHost: true, hostWorker: true })
  })

  it('api: the HTTP half and its own host queue, no agent-sessions, shared agent-tools or session host worker', () => {
    expect(roleParts('api')).toEqual({ http: true, sharedWorkers: false, sessionHost: false, hostWorker: true })
  })

  it('worker: the Temporal workers that run turns, and no HTTP routes', () => {
    expect(roleParts('worker')).toEqual({ http: false, sharedWorkers: true, sessionHost: true, hostWorker: true })
  })
})

describe("the worker's HTTP server", () => {
  function app(ready: string | undefined, database: boolean | null = true) {
    return workerHealthApp({
      ...(database === null ? {} : { database: { ping: () => Promise.resolve(database) } }),
      kek: { ok: true },
      workers: () => ({ 'agent-tools': 'ok', 'agent-sessions': 'ok', 'agent-sessions.w-1': 'connecting', 'agent-tools.w-1': 'ok' }),
      ready: () => Promise.resolve(ready),
    })
  }

  it.each(['/api/v1/ai/status', '/api/v1/ai/chat', '/api/v1/ai/sessions', '/mcp', '/internal/flows/tools'])(
    'answers 404 on %s',
    async (path) => {
      expect((await app(undefined).request(path)).status).toBe(404)
    },
  )

  it('reports its role, the database, the key and each queue on /healthz, always 200', async () => {
    const res = await app('not yet', false).request('/healthz')
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({
      status: 'ok',
      role: 'worker',
      database: 'unreachable',
      secret_key: 'ok',
      temporal: 'connecting',
      queues: { 'agent-tools': 'ok', 'agent-sessions': 'ok', 'agent-sessions.w-1': 'connecting', 'agent-tools.w-1': 'ok' },
    } satisfies WorkerHealth)
  })

  it('says the database is not configured without one', async () => {
    expect(((await (await app(undefined, null).request('/healthz')).json()) as WorkerHealth).database).toBe('not configured')
  })

  it('is ready on /readyz only once every queue it serves is polled', async () => {
    const waiting = await app('no poller of this worker on "agent-sessions.w-1" yet').request('/readyz')
    expect(waiting.status).toBe(503)
    expect(await waiting.json()).toEqual({ ready: false, reason: 'no poller of this worker on "agent-sessions.w-1" yet' })
    const ready = await app(undefined).request('/readyz')
    expect(ready.status).toBe(200)
    expect(await ready.json()).toEqual({ ready: true })
  })
})

describe("the worker's readiness", () => {
  const ME = '7@worker-1'
  const secondsAgo = (s: number) => ({ seconds: Math.floor(Date.now() / 1000) - s })
  const QUEUES = [
    { name: 'agent-sessions', type: 1 },
    { name: 'agent-tools', type: 2 },
    { name: 'agent-sessions.worker-1', type: 2 },
  ] as const

  function client(pollers: (queue: string, type: number) => { identity?: string; lastAccessTime?: { seconds: number } }[] | Error) {
    return {
      options: { namespace: 'default' },
      withDeadline: <R>(_deadline: number, fn: () => Promise<R>) => fn(),
      workflowService: {
        describeTaskQueue: ({ taskQueue, taskQueueType }: { taskQueue: { name: string }; taskQueueType: number }) => {
          const answer = pollers(taskQueue.name, taskQueueType)
          return answer instanceof Error ? Promise.reject(answer) : Promise.resolve({ pollers: answer })
        },
      },
    } as unknown as Parameters<typeof queuesPolled>[0]
  }

  it('is ready once this process polls every queue it serves', async () => {
    expect(await queuesPolled(client(() => [{ identity: ME, lastAccessTime: secondsAgo(2) }]), QUEUES, ME)).toBeUndefined()
  })

  it('is not ready while another process is the only poller of one, or its own poll is stale', async () => {
    const other = client((queue) => [{ identity: queue === 'agent-tools' ? '9@worker-2' : ME, lastAccessTime: secondsAgo(2) }])
    expect(await queuesPolled(other, QUEUES, ME)).toBe(`this worker does not poll Temporal's "agent-tools" queue yet`)
    const stale = client((queue) => [{ identity: ME, lastAccessTime: secondsAgo(queue === 'agent-sessions.worker-1' ? 300 : 2) }])
    expect(await queuesPolled(stale, QUEUES, ME)).toMatch(/agent-sessions\.worker-1/)
  })

  it('reads the workflow queue as a workflow queue', async () => {
    const byType = client((queue, type) => (queue === 'agent-sessions' && type !== 1 ? [] : [{ identity: ME, lastAccessTime: secondsAgo(2) }]))
    expect(await queuesPolled(byType, QUEUES, ME)).toBeUndefined()
  })

  it('is not ready when Temporal does not answer', async () => {
    expect(await queuesPolled(client(() => new Error('unavailable')), QUEUES, ME)).toBe('Temporal did not answer')
  })

  it('names a queue this process never started a worker on, without asking Temporal', async () => {
    let asked = 0
    const counting = client(() => {
      asked++
      return [{ identity: ME, lastAccessTime: secondsAgo(2) }]
    })
    const queues = QUEUES.map((q) => ({ ...q, started: q.name !== 'agent-sessions.worker-1' }))
    expect(await queuesPolled(counting, queues, ME)).toBe(
      `this process started no worker on "agent-sessions.worker-1"; its start log says why`,
    )
    expect(asked).toBe(0)
  })

  it('gives each describe call its own deadline', async () => {
    const deadlines: number[] = []
    const slow = {
      options: { namespace: 'default' },
      withDeadline: async <R>(deadline: number, fn: () => Promise<R>) => {
        deadlines.push(deadline)
        await new Promise((r) => setTimeout(r, 30))
        return fn()
      },
      workflowService: { describeTaskQueue: () => Promise.resolve({ pollers: [{ identity: ME, lastAccessTime: secondsAgo(2) }] }) },
    } as unknown as Parameters<typeof queuesPolled>[0]
    expect(await queuesPolled(slow, QUEUES, ME)).toBeUndefined()
    expect(deadlines[2]! - deadlines[0]!).toBeGreaterThanOrEqual(50)
  })
})
