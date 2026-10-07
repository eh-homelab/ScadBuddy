import { type Client, WorkflowUpdateRPCTimeoutOrCancelledError } from '@temporalio/client'
import { describe, expect, it } from 'vitest'
import { CommandStillAcceptingError, startCommand } from '../src/operations/command.js'

// startCommand's deadlines (#1056 final fix wave): timers on a monotonic clock, never a
// wall-clock deadline (this host's wall clock steps by seconds). The command itself runs
// on Temporal in test/agentOperation.temporal.test.ts.

/** A Client whose update-with-start hangs until its abort signal fires, and that refuses wall-clock deadlines. */
function hangingClient() {
  const signals: AbortSignal[] = []
  const hang = (signal: AbortSignal | undefined) =>
    new Promise<never>((_, reject) => {
      signal?.addEventListener('abort', () => reject(Object.assign(new Error('1 CANCELLED'), { code: 1 })), { once: true })
    })
  const client = {
    connection: {
      withDeadline: () => {
        throw new Error('a wall-clock deadline (withDeadline) was used')
      },
      withAbortSignal: <T>(signal: AbortSignal, fn: () => Promise<T>) => {
        signals.push(signal)
        return fn()
      },
    },
    workflow: {
      // As the SDK answers an Update call its signal cancelled.
      executeUpdateWithStart: () =>
        hang(signals.at(-1)).catch(() => {
          throw new WorkflowUpdateRPCTimeoutOrCancelledError('Workflow update call timeout or cancelled')
        }),
      getHandle: () => ({ describe: async () => ({}) }),
    },
  }
  return { client: client as unknown as Client, signals }
}

describe('startCommand', () => {
  it('bounds the update-with-start and the describe after it with timers, not wall-clock deadlines', async () => {
    const { client, signals } = hangingClient()
    const started = performance.now()
    await expect(startCommand(client, 'op-1', { kind: 'k', args: {} } as never, 50)).rejects.toBeInstanceOf(
      CommandStillAcceptingError,
    )
    expect(performance.now() - started).toBeLessThan(10_000)
    expect(signals).toHaveLength(2)
    expect(signals[0]!.aborted).toBe(true)
  })
})
