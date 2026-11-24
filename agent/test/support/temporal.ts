import type { Client } from '@temporalio/client'
import { TestWorkflowEnvironment } from '@temporalio/testing'
import { execFileSync } from 'node:child_process'
import { type QueueDescriber, queuePollers } from '../../src/temporal/pollers.js'
import { HOST_FRESH_MS } from '../../src/temporal/session/affinity.js'

// A Temporal dev server for the agent's `agent-tools` tests (spec 2026-10-01 §8, #1055),
// as the backend's `requires_temporal` tests get one: SCADBUDDY_TEST_TEMPORAL_DEV_SERVER
// names a Temporal CLI, else `temporal` on PATH; without either the tests skip. Never
// downloaded: CI installs the pinned CLI (ci.yml, the agent job).

export const TEMPORAL_CLI_ENV = 'SCADBUDDY_TEST_TEMPORAL_DEV_SERVER'

function onPath(): string | undefined {
  try {
    return execFileSync('which', ['temporal'], { encoding: 'utf8' }).trim() || undefined
  } catch {
    return undefined
  }
}

export const TEMPORAL_CLI = process.env[TEMPORAL_CLI_ENV]?.trim() || onPath()
export const TEMPORAL_SKIP = TEMPORAL_CLI ? '' : ` (skipped: ${TEMPORAL_CLI_ENV} is not set and no temporal on PATH)`

/**
 * `deleteAtOnce`: `deleteWorkflowExecution` removes a closed run in milliseconds, not 19-33 s.
 * By default the server holds the delete task until the visibility queue's checkpoint has
 * passed the run's close task (`history.transferProcessorEnsureCloseBeforeDelete`), and that
 * checkpoint moves about every 30 s, so a test that waits for the run to go raced it (#5243;
 * measured on CLI 1.9.1, Server 1.32.0: 19-33 s with the check, 9-18 ms without).
 */
export function localTemporal(opts: { deleteAtOnce?: boolean } = {}): Promise<TestWorkflowEnvironment> {
  return TestWorkflowEnvironment.createLocal({
    server: {
      executable: { type: 'existing-path', path: TEMPORAL_CLI! },
      extraArgs: opts.deleteAtOnce ? ['--dynamic-config-value', 'history.transferProcessorEnsureCloseBeforeDelete=false'] : [],
    },
  })
}

/**
 * Which of `queues` (session host queues) a worker polls now: DurableTurns' `liveHosts` in a test,
 * where the real listing (`liveHostQueues`) would also name other tests' replicas.
 */
export function polledHosts(client: Pick<Client, 'workflowService' | 'options' | 'withDeadline'>, ...queues: string[]): () => Promise<ReadonlySet<string>> {
  return async () => {
    const live = new Set<string>()
    for (const queue of queues) {
      // As liveHostQueues: every partition, within POLLER_READ_MS.
      const pollers = await queuePollers(client, queue, 2, Date.now() + POLLER_READ_MS)
      if (pollers.some((p) => Date.now() - p.seenAtMs < HOST_FRESH_MS)) live.add(queue)
    }
    return live
  }
}

/** How long a test's poller read may take: a dev server that hangs fails the read, not the test's whole timeout. */
const POLLER_READ_MS = 10_000

/** The identities that poll `queue` (1 workflow, 2 activity) on any partition now (temporal/pollers.ts, #7306). */
export async function pollerIdentities(client: QueueDescriber, queue: string, type: number): Promise<string[]> {
  return (await queuePollers(client, queue, type, Date.now() + POLLER_READ_MS)).flatMap((p) => (p.identity ? [p.identity] : []))
}

/** Whether any worker polls `queue` (1 workflow, 2 activity) on any partition now (temporal/pollers.ts, #7306). */
export async function anyPoller(client: QueueDescriber, queue: string, type: number): Promise<boolean> {
  return (await queuePollers(client, queue, type, Date.now() + POLLER_READ_MS)).length > 0
}
