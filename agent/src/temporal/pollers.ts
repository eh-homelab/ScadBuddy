import type { Client } from '@temporalio/client'

/**
 * The read partitions of a normal task queue: Temporal's default
 * `matching.numTaskqueueReadPartitions`. Prod's dynamic config does not set it
 * (`scadbuddy-temporal-v2-dynamicconfig`, read 2026-11-24), and the dev server the tests run
 * has these four too (#7306).
 */
export const TASK_QUEUE_PARTITIONS = 4

export type QueueDescriber = Pick<Client, 'workflowService' | 'options' | 'withDeadline'>

/** A worker that polls a queue: its identity, when Temporal reports one, and when it last polled (epoch ms). */
export type QueuePoller = { identity?: string; seenAtMs: number }

/** A protobuf timestamp (a poller's `lastAccessTime`) as epoch milliseconds; 0 when absent. */
export function seenAtMs(ts: { seconds?: unknown; nanos?: number | null } | null | undefined): number {
  return ts ? Number(String(ts.seconds ?? 0)) * 1000 + Math.floor((ts.nanos ?? 0) / 1e6) : 0
}

/** The name Temporal gives partition `n` of `queue`; partition 0 is the queue's own name. */
export const partitionName = (queue: string, n: number): string => (n === 0 ? queue : `/_sys/${queue}/${n}`)

/**
 * The pollers of every partition of `queue` (of `type`, temporal.api.enums.v1.TaskQueueType),
 * one per identity at its latest poll; pollers without an identity are kept as they are.
 * DescribeTaskQueue reports one partition's pollers, the root's when asked by the queue's name,
 * and a worker's long polls land on any partition: on 2026-11-24 a live worker polled
 * `agent-tools` partitions 1 to 3 for 30 s and was absent from the root's list (#7306). The
 * deprecated ENHANCED mode, which merged them, is not used. Rejects when any partition's
 * answer fails.
 */
export async function queuePollers(client: QueueDescriber, queue: string, type: number, deadline: number): Promise<QueuePoller[]> {
  const answers = await client.withDeadline(deadline, () =>
    Promise.all(
      Array.from({ length: TASK_QUEUE_PARTITIONS }, (_, n) =>
        client.workflowService.describeTaskQueue({
          namespace: client.options.namespace,
          taskQueue: { name: partitionName(queue, n) },
          taskQueueType: type,
        }),
      ),
    ),
  )
  const byIdentity = new Map<string, QueuePoller>()
  const anonymous: QueuePoller[] = []
  for (const { pollers } of answers) {
    for (const p of pollers ?? []) {
      const seen = seenAtMs(p.lastAccessTime)
      if (!p.identity) {
        anonymous.push({ seenAtMs: seen })
        continue
      }
      const known = byIdentity.get(p.identity)
      if (!known || known.seenAtMs < seen) byIdentity.set(p.identity, { identity: p.identity, seenAtMs: seen })
    }
  }
  return [...byIdentity.values(), ...anonymous]
}
