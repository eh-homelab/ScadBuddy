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
