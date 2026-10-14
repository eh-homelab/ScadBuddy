import {
  CancellationScope,
  condition,
  continueAsNew,
  defineSignal,
  isCancellation,
  proxyActivities,
  setHandler,
  sleep,
  workflowInfo,
} from '@temporalio/workflow'
import type { DeliverResult, FireResult, MonitorPlan, MonitorResult, NextStep } from '../background/activities.js'
import type { MonitorEnd } from '../background/monitor.js'

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
  bg_monitor_plan(input: { taskId: string }): Promise<MonitorPlan>
  bg_monitor_end(input: { taskId: string; end: MonitorEnd }): Promise<void>
  bg_deliver(input: { taskId: string; waitedMs: number }): Promise<DeliverResult>
}

type Source = (input: { taskId: string }) => Promise<MonitorResult>

/** A monitor's source recorded an event (background/activities.ts BG_EVENTS_SIGNAL). */
export const eventsSignal = defineSignal('bg_events')

const acts = proxyActivities<Activities>({
  startToCloseTimeout: '1 minute',
  retry: { initialInterval: '1s', maximumInterval: '30s', backoffCoefficient: 2 },
})

/** How often a due task asks a busy session again. */
export const BUSY_RETRY_MS = 30_000

/** `retryMs`: how often a busy session is asked again (BUSY_RETRY_MS; tests set less). */
export async function BackgroundTask(input: { taskId: string; kind?: string; retryMs?: number }): Promise<void> {
  const { taskId } = input
  const retryMs = input.retryMs ?? BUSY_RETRY_MS
  try {
    if (input.kind === 'monitor') {
      await monitor(taskId, retryMs)
      return
    }
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
        await sleep(retryMs)
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

/**
 * A monitor: its source runs as one heartbeating activity on the queue the plan names
 * (where the session's Bash runs, or any agent for a WebSocket), recording each event
 * and signalling this workflow; each signal sends what is undelivered as the session's
 * next message once it is idle. Its end is the last event, sent the same way.
 */
async function monitor(taskId: string, retryMs: number): Promise<void> {
  const plan = await acts.bg_monitor_plan({ taskId })
  if ('end' in plan) {
    await acts.bg_finish({ taskId, state: 'done', detail: plan.end })
    return
  }
  let pending = false
  setHandler(eventsSignal, () => {
    pending = true
  })
  const sources = proxyActivities<Record<string, Source>>({
    taskQueue: plan.queue,
    // A classic command's queue is one replica's: if it is gone before it starts the source, the monitor fails.
    scheduleToStartTimeout: '5 minutes',
    startToCloseTimeout: plan.timeoutMs + 120_000,
    heartbeatTimeout: '30 seconds',
    retry: { maximumAttempts: 1 },
  })
  let ended: MonitorEnd | undefined
  const scope = new CancellationScope()
  const source = scope
    .run(() => sources[plan.activity]!({ taskId }))
    .then(
      (r) => (ended = r.end),
      (err: unknown) => {
        if (!isCancellation(err)) ended = { outcome: 'failed', reason: err instanceof Error ? err.message : String(err) }
      },
    )
  let waitingSince: number | undefined
  while (ended === undefined) {
    await condition(() => pending || ended !== undefined, waitingSince === undefined ? undefined : retryMs)
    pending = false
    const sent = await acts.bg_deliver({ taskId, waitedMs: waitingSince === undefined ? 0 : Date.now() - waitingSince })
    if (sent.result === 'end') {
      // The session takes no more messages (or stayed busy past the give-up): the watch ends with it.
      scope.cancel()
      await source
      await acts.bg_finish({ taskId, state: sent.state, detail: sent.reason })
      return
    }
    // Busy: ask again after retryMs, events or not; delivered with more left: deliver
    // again (busy until its turn ends); delivered (or nothing): wait for the next.
    waitingSince = sent.result === 'busy' ? (waitingSince ?? Date.now()) : undefined
    if (sent.result === 'sent' && sent.more) pending = true
  }
  await acts.bg_monitor_end({ taskId, end: ended })
  const due = Date.now()
  for (;;) {
    const sent = await acts.bg_deliver({ taskId, waitedMs: Date.now() - due })
    if (sent.result === 'none') break
    if (sent.result === 'end') {
      await acts.bg_finish({ taskId, state: sent.state, detail: sent.reason })
      return
    }
    if (sent.result === 'busy') await sleep(retryMs)
  }
  await acts.bg_finish({ taskId, state: 'done', detail: ended.outcome })
}
