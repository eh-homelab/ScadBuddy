import type { Client } from '@temporalio/client'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { DurableTurns } from '../src/sessions/durable.js'
import type { EventLog } from '../src/sessions/eventLog.js'

// DurableTurns.unready (plan 5d Ruling 2b) against a stubbed DescribeTaskQueue: only a
// poller Temporal saw lately counts, since it lists one for minutes after its worker went.

function turns(pollers: { lastAccessTime?: { seconds: number; nanos?: number } }[] | Error) {
  let calls = 0
  const client = {
    options: { namespace: 'default' },
    withDeadline: <R>(_deadline: number, fn: () => Promise<R>) => fn(),
    workflowService: {
      describeTaskQueue: () => {
        calls++
        return pollers instanceof Error ? Promise.reject(pollers) : Promise.resolve({ pollers })
      },
    },
  } as unknown as Client
  const t = new DurableTurns({ client, sql: {} as Sql, events: {} as EventLog })
  return { t, calls: () => calls }
}

const secondsAgo = (s: number) => ({ seconds: Math.floor(Date.now() / 1000) - s })

describe('whether a durable session can start now', () => {
  it('is ready with a poller seen just now, and reuses the answer', async () => {
    const { t, calls } = turns([{ lastAccessTime: secondsAgo(5) }])
    expect(await t.unready()).toBeUndefined()
    expect(await t.unready()).toBeUndefined()
    expect(calls()).toBe(1)
  })

  it('is not ready with only a poller Temporal still lists after its worker went', async () => {
    const { t } = turns([{ lastAccessTime: secondsAgo(300) }, {}])
    expect(await t.unready()).toMatch(/no durable session worker/)
  })

  it('is not ready when Temporal does not answer', async () => {
    const { t } = turns(new Error('unavailable'))
    expect(await t.unready()).toBe('Temporal did not answer')
  })
})

// A unified session (AgentSession, spec 2026-10-13 §5.1-5.2) needs the `agent-sessions` workflow
// queue and the shared `agent-tools` activity queue (acquire_worker) polled, and at least one live
// session host anywhere (`liveHostQueues`): the API's process hosts none of its own (§14.3, plan
// Step 6.1), so its own queue is never asked for. agent-durable's `agent` is not one of them.
describe('whether a unified session can start now', () => {
  const NEEDED = [
    { queue: 'agent-sessions', type: 1 },
    { queue: 'agent-tools', type: 2 },
  ]

  function unified(missing: { queue: string; type: number } | undefined, options: { hosts?: string[] | Error } = {}) {
    const asked: string[] = []
    let listed = 0
    const client = {
      options: { namespace: 'default' },
      withDeadline: <R>(_deadline: number, fn: () => Promise<R>) => fn(),
      workflowService: {
        describeTaskQueue: ({ taskQueue, taskQueueType }: { taskQueue: { name: string }; taskQueueType: number }) => {
          asked.push(`${taskQueue.name}/${taskQueueType}`)
          const gone = missing && missing.queue === taskQueue.name && missing.type === taskQueueType
          return Promise.resolve({ pollers: gone ? [{ lastAccessTime: secondsAgo(300) }] : [{ lastAccessTime: secondsAgo(5) }] })
        },
      },
    } as unknown as Client
    const hosts = options.hosts ?? ['agent-sessions.worker-a']
    const liveHosts = () => {
      listed++
      return hosts instanceof Error ? Promise.reject(hosts) : Promise.resolve(new Set(hosts))
    }
    return { t: new DurableTurns({ client, sql: {} as Sql, events: {} as EventLog, liveHosts }), asked, listed: () => listed }
  }

  it('is ready with fresh pollers on both shared queues and a live session host, and never asks about `agent`', async () => {
    const { t, asked, listed } = unified(undefined)
    expect(await t.unready('unified')).toBeUndefined()
    expect(asked).toEqual(NEEDED.map((n) => `${n.queue}/${n.type}`))
    expect(listed()).toBe(1)
  })

  it.each(NEEDED)('is not ready without a fresh poller on $queue (type $type)', async (missing) => {
    const { t } = unified(missing)
    expect(await t.unready('unified')).toMatch(new RegExp(`polls Temporal's "${missing.queue}" queue`))
  })

  it('is not ready while no session host polls anywhere, though this process hosts none itself', async () => {
    const { t } = unified(undefined, { hosts: [] })
    expect(await t.unready('unified')).toMatch(/no session host worker \(agent-sessions\.<identity>\) polls Temporal/)
  })

  it('is not ready when the session hosts cannot be listed', async () => {
    const { t } = unified(undefined, { hosts: new Error('unavailable') })
    expect(await t.unready('unified')).toBe('Temporal did not answer')
  })

  it('keeps the durable answer apart: `agent` is still what a DurableSession needs', async () => {
    const { t, asked } = unified({ queue: 'agent-sessions', type: 1 })
    expect(await t.unready('durable')).toBeUndefined()
    expect(asked).toEqual(['agent/1'])
    expect(await t.unready('unified')).toMatch(/agent-sessions/)
  })
})
