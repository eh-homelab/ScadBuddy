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

// A unified session (AgentSession, spec 2026-10-13 §5.1-5.2) needs three queues polled: the
// `agent-sessions` workflow queue, the shared `agent-tools` activity queue (acquire_worker) and
// this replica's own session host queue; agent-durable's `agent` is not one of them.
describe('whether a unified session can start now', () => {
  const HOST = 'agent-sessions.replica-a'
  const NEEDED = [
    { queue: 'agent-sessions', type: 1 },
    { queue: 'agent-tools', type: 2 },
    { queue: HOST, type: 2 },
  ]

  function unified(missing: { queue: string; type: number } | undefined, options: { host?: string | null } = {}) {
    const asked: string[] = []
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
    const host = options.host === null ? {} : { sessionHostQueue: options.host ?? HOST }
    return { t: new DurableTurns({ client, sql: {} as Sql, events: {} as EventLog, ...host }), asked }
  }

  it('is ready with fresh pollers on the three queues, and never asks about `agent`', async () => {
    const { t, asked } = unified(undefined)
    expect(await t.unready('unified')).toBeUndefined()
    expect(asked).toEqual(NEEDED.map((n) => `${n.queue}/${n.type}`))
  })

  it.each(NEEDED)('is not ready without a fresh poller on $queue (type $type)', async (missing) => {
    const { t } = unified(missing)
    expect(await t.unready('unified')).toMatch(new RegExp(`polls Temporal's "${missing.queue}" queue`))
  })

  it('is not ready on a replica that serves no session host queue', async () => {
    const { t, asked } = unified(undefined, { host: null })
    expect(await t.unready('unified')).toMatch(/no session host worker/)
    expect(asked).toEqual([])
  })

  it('keeps the durable answer apart: `agent` is still what a DurableSession needs', async () => {
    const { t, asked } = unified({ queue: 'agent-sessions', type: 1 })
    expect(await t.unready('durable')).toBeUndefined()
    expect(asked).toEqual(['agent/1'])
    expect(await t.unready('unified')).toMatch(/agent-sessions/)
  })
})
