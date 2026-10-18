import type { Context as ActivityContext } from '@temporalio/activity'
import type { ActivityExecuteInput } from '@temporalio/worker'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ACTIVITY_HEARTBEAT_MS, heartbeatingActivities } from '../src/temporal/worker.js'

// Every agent-tools activity beats from its first moment (#4199), so a caller's
// heartbeat timeout notices an attempt that was never delivered long before the
// call's start-to-close would.

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

function intercepted(heartbeat: () => void) {
  const inbound = heartbeatingActivities()({ heartbeat } as unknown as ActivityContext).inbound
  if (!inbound?.execute) throw new Error('no inbound execute')
  return inbound.execute.bind(inbound)
}

it('beats while an activity runs and stops when it ends', async () => {
  const heartbeat = vi.fn()
  let finish!: (value: string) => void
  const execute = intercepted(heartbeat)
  const running = execute({} as ActivityExecuteInput, () => new Promise<string>((resolve) => (finish = resolve)))
  await vi.advanceTimersByTimeAsync(ACTIVITY_HEARTBEAT_MS * 3)
  expect(heartbeat).toHaveBeenCalledTimes(3)
  finish('done')
  await expect(running).resolves.toBe('done')
  await vi.advanceTimersByTimeAsync(ACTIVITY_HEARTBEAT_MS * 3)
  expect(heartbeat).toHaveBeenCalledTimes(3)
})

it('stops beating when the activity fails', async () => {
  const heartbeat = vi.fn()
  const execute = intercepted(heartbeat)
  await expect(execute({} as ActivityExecuteInput, () => Promise.reject(new Error('boom')))).rejects.toThrow('boom')
  await vi.advanceTimersByTimeAsync(ACTIVITY_HEARTBEAT_MS * 3)
  expect(heartbeat).not.toHaveBeenCalled()
})
