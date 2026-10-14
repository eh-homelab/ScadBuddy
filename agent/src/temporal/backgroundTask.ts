import { CancellationScope, continueAsNew, isCancellation, proxyActivities, sleep, workflowInfo } from '@temporalio/workflow'
import type { FireResult, NextStep } from '../background/activities.js'

// A session's background task (src/background/): a cron job or a wakeup, fired as the
// session's next message when it is due and the session is idle, as Claude Code fires
// its own "only while the REPL is idle". Claude Code keeps those in its process, which a
// turn ends; this workflow keeps them for as long as they last, in either session mode.
//
// It carries only the task's id. Each step reads the row (background/activities.ts), so
// a cancel, which ends the row first, stops the next fire wherever this is waiting.
// A change to this function goes behind `patched()`.

type Activities = {
  bg_next(input: { taskId: string }): Promise<NextStep>
  bg_fire(input: { taskId: string; waitedMs: number }): Promise<FireResult>
  bg_finish(input: { taskId: string; state: 'done' | 'cancelled' | 'failed'; detail?: string }): Promise<void>
}

const acts = proxyActivities<Activities>({
  startToCloseTimeout: '1 minute',
  retry: { initialInterval: '1s', maximumInterval: '30s', backoffCoefficient: 2 },
})

/** How often a due task asks a busy session again. */
export const BUSY_RETRY_MS = 30_000

/** `retryMs`: how often a busy session is asked again (BUSY_RETRY_MS; tests set less). */
export async function BackgroundTask(input: { taskId: string; retryMs?: number }): Promise<void> {
  const { taskId } = input
  try {
    for (;;) {
      const step = await acts.bg_next({ taskId })
      if ('end' in step) {
        await acts.bg_finish({ taskId, state: 'done', detail: step.end })
        return
      }
      const wait = step.at - Date.now()
      if (wait > 0) await sleep(wait)
      const due = Date.now()
      for (;;) {
        const fired = await acts.bg_fire({ taskId, waitedMs: Date.now() - due })
        if (fired.result === 'sent' || fired.result === 'skipped') break
        if (fired.result === 'end') {
          await acts.bg_finish({ taskId, state: fired.state, detail: fired.reason })
          return
        }
        await sleep(input.retryMs ?? BUSY_RETRY_MS)
      }
      // A job every minute for 7 days is ~10,000 fires: start a fresh history first.
      if (workflowInfo().continueAsNewSuggested) await continueAsNew<typeof BackgroundTask>(input)
    }
  } catch (err) {
    if (!isCancellation(err)) throw err
    // The row was ended by the cancel already (service.ts); this covers a cancel from
    // elsewhere (the Temporal UI, an operator).
    await CancellationScope.nonCancellable(() => acts.bg_finish({ taskId, state: 'cancelled', detail: 'cancelled' }))
  }
}
