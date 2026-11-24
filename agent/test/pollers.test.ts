import { describe, expect, it } from 'vitest'
import { pollingReplicas } from '../src/plugins/pluginSessions.js'
import { queuesPolled } from '../src/runtime/health.js'
import { liveHostQueues } from '../src/temporal/session/affinity.js'
import { queuePollers, TASK_QUEUE_PARTITIONS } from '../src/temporal/pollers.js'

// DescribeTaskQueue reports one partition's pollers, and a worker's long polls land on any
// partition: on 2026-11-24 a live replica polled `agent-tools` partitions 1 to 3 and was absent
// from the root's list, so `liveHostQueues` did not name it (#7306).

type Poller = { identity?: string; lastAccessTime?: { seconds: number } }
const secondsAgo = (s: number) => ({ seconds: Math.floor(Date.now() / 1000) - s })

/** A dev server whose pollers are listed by partition name (`queue`, `/_sys/queue/1`, ...). */
function server(partitions: Record<string, Poller[]>) {
  const asked: string[] = []
  const client = {
    options: { namespace: 'default' },
    withDeadline: <R>(_deadline: number, fn: () => Promise<R>) => fn(),
    workflowService: {
      describeTaskQueue: ({ taskQueue }: { taskQueue: { name: string } }) => {
        asked.push(taskQueue.name)
        return Promise.resolve({ pollers: partitions[taskQueue.name] ?? [] })
      },
    },
  }
  return { client: client as never, asked }
}

describe('a queue poller seen only on a partition other than the root', () => {
  const B = '3891@b-0c22502c'
  const onlyPartition2 = {
    'agent-tools': [{ identity: '3891@a-0c22502c', lastAccessTime: secondsAgo(30) }],
    '/_sys/agent-tools/2': [{ identity: B, lastAccessTime: secondsAgo(30) }],
    'agent-sessions.a-0c22502c': [{ identity: '3891@a-0c22502c', lastAccessTime: secondsAgo(30) }],
    'agent-sessions.b-0c22502c': [{ identity: B, lastAccessTime: secondsAgo(30) }],
  }

  it('is listed live by liveHostQueues', async () => {
    const { client } = server(onlyPartition2)
    expect(await liveHostQueues(client)).toEqual(new Set(['agent-sessions.a-0c22502c', 'agent-sessions.b-0c22502c']))
  })

  it('counts as a polling replica, so the plugin session sweep keeps its sessions', async () => {
    const { client } = server(onlyPartition2)
    expect(await pollingReplicas(client)).toEqual(new Set(['a-0c22502c', 'b-0c22502c']))
  })

  it('makes its process ready', async () => {
    const { client } = server(onlyPartition2)
    expect(await queuesPolled(client, [{ name: 'agent-tools', type: 2 }], B)).toBeUndefined()
  })
})

describe('queuePollers', () => {
  it('asks every partition and keeps each identity once, at its latest poll', async () => {
    const { client, asked } = server({
      q: [{ identity: 'w', lastAccessTime: { seconds: 1_000 } }, {}],
      '/_sys/q/1': [{ identity: 'w', lastAccessTime: { seconds: 1_045 } }],
      '/_sys/q/3': [{ identity: 'v', lastAccessTime: { seconds: 1_041 } }, { lastAccessTime: { seconds: 1_048 } }],
    })
    const pollers = await queuePollers(client, 'q', 2, Date.now() + 1_000)
    expect(asked).toEqual(['q', '/_sys/q/1', '/_sys/q/2', '/_sys/q/3'])
    expect(asked).toHaveLength(TASK_QUEUE_PARTITIONS)
    expect(pollers).toEqual([
      { identity: 'w', seenAtMs: 1_045_000 },
      { identity: 'v', seenAtMs: 1_041_000 },
      { seenAtMs: 0 },
      { seenAtMs: 1_048_000 },
    ])
  })

  it('fails when one partition does not answer', async () => {
    const client = {
      options: { namespace: 'default' },
      withDeadline: <R>(_deadline: number, fn: () => Promise<R>) => fn(),
      workflowService: {
        describeTaskQueue: ({ taskQueue }: { taskQueue: { name: string } }) =>
          taskQueue.name === '/_sys/q/2' ? Promise.reject(new Error('unavailable')) : Promise.resolve({ pollers: [] }),
      },
    }
    await expect(queuePollers(client as never, 'q', 2, Date.now() + 1_000)).rejects.toThrow('unavailable')
  })
})
