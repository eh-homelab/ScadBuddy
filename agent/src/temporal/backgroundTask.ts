import {
  ActivityCancellationType,
  CancellationScope,
  condition,
  continueAsNew,
  defineSignal,
  isCancellation,
  patched,
  proxyActivities,
  setHandler,
  sleep,
  workflowInfo,
} from '@temporalio/workflow'
import type { DeliverResult, FireResult, MonitorPlan, MonitorResult, NextStep, WorkflowOutcome } from '../background/activities.js'
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
  wf_end(input: { taskId: string; outcome: Exclude<WorkflowOutcome, { status: 'gone' }> }): Promise<void>
  bg_schedule(input: { taskId: string }): Promise<boolean>
  bg_unschedule(input: { taskId: string; sessionId: string }): Promise<void>
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

/** A recurring cron job's workflow hands its fires to a Schedule (plan 2026-10-13 Step 9.8). */
export const CRON_SCHEDULE_PATCH = 'cron-schedule-9.8'

/** `retryMs`: how often a busy session is asked again (BUSY_RETRY_MS; tests set less). */
export async function BackgroundTask(input: { taskId: string; kind?: string; recurring?: boolean; retryMs?: number }): Promise<void> {
  const { taskId } = input
  const retryMs = input.retryMs ?? BUSY_RETRY_MS
  try {
    if (input.kind === 'monitor') {
      await monitor(taskId, retryMs)
      return
    }
    if (input.kind === 'workflow') {
      await scriptRun(taskId, retryMs)
      return
    }
    for (;;) {
      // A recurring cron job is a Schedule from Step 9.8 on: one made before it becomes one at its
      // next occurrence (and the start-time pass converts the rest, background/temporal.ts). A
      // one-shot job, started with `recurring: false`, never asks; one started before it may.
      if (input.kind === 'cron' && input.recurring !== false && patched(CRON_SCHEDULE_PATCH) && (await acts.bg_schedule({ taskId }))) return
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

/** The session and task a Schedule's fire is for, from its workflow id: `bgfire-<session>-<task>-<fire time>` (background/service.ts). */
export function fireTask(workflowId: string): { sessionId: string; taskId: string } | undefined {
  const m = /^bgfire-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-([0-9a-f]{8})-/.exec(workflowId)
  return m ? { sessionId: m[1]!, taskId: m[2]! } : undefined
}

/**
 * One occurrence of a recurring cron job, started by its Schedule (plan 2026-10-13 Step 9.8):
 * sends the prompt once the session is idle (asked again every `retryMs`; given up after an
 * hour busy), then records the next occurrence. A task that ends (cancelled, its session gone,
 * past its 7 days) ends its row and its Schedule.
 */
export async function BackgroundFire(input?: { retryMs?: number }): Promise<void> {
  const fire = fireTask(workflowInfo().workflowId)
  if (fire === undefined) return
  const { taskId } = fire
  const retryMs = input?.retryMs ?? BUSY_RETRY_MS
  const due = Date.now()
  for (;;) {
    const fired = await acts.bg_fire({ taskId, waitedMs: Date.now() - due })
    if (fired.result === 'sent' || fired.result === 'skipped') break
    if (fired.result === 'end') {
      await acts.bg_finish({ taskId, state: fired.state, detail: fired.reason })
      await acts.bg_unschedule(fire)
      return
    }
    await sleep(retryMs)
  }
  const step = await acts.bg_next({ taskId })
  if ('end' in step) {
    await acts.bg_finish({ taskId, state: 'done', detail: step.end })
    await acts.bg_unschedule(fire)
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
    // A stopped watch ends only once its source has stopped (it hears the cancel at its next
    // heartbeat, throttled to most of heartbeatTimeout), so its command never outlives the task.
    cancellationType: ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
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
    const woken = () => pending || ended !== undefined
    // The same as passing an undefined timeout, which `condition`'s types no longer take.
    await (waitingSince === undefined ? condition(woken) : condition(woken, retryMs))
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

/** A Workflow script's run: as long as its agents take, past any one worker (its journal resumes it). */
const scripts = proxyActivities<{ wf_run(input: { taskId: string }): Promise<WorkflowOutcome> }>({
  startToCloseTimeout: '6 hours',
  // wf_run stops itself at the task's expiry (6 hours after it was started); this bounds the
  // retries of one whose worker died, which would otherwise each get 6 hours more.
  scheduleToCloseTimeout: '370 minutes',
  heartbeatTimeout: '1 minute',
  retry: { initialInterval: '5s', maximumAttempts: 3 },
})

/**
 * A Workflow (Claude Code's): its script runs as one heartbeating activity
 * (background/activities.ts `wf_run`, in QuickJS), each agent() a child chat; its end is
 * Claude Code's notice, sent as the session's next message once it is idle.
 */
async function scriptRun(taskId: string, retryMs: number): Promise<void> {
  let outcome: WorkflowOutcome
  try {
    outcome = await scripts.wf_run({ taskId })
  } catch (err) {
    if (isCancellation(err)) throw err
    const zero = { agents: 0, done: 0, errors: 0, empty: 0, durationMs: 0 }
    outcome = { status: 'failed', error: err instanceof Error ? err.message : String(err), usage: zero }
  }
  if (outcome.status === 'gone') {
    await acts.bg_finish({ taskId, state: 'done', detail: 'gone' })
    return
  }
  await acts.wf_end({ taskId, outcome })
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
  const detail = outcome.status === 'failed' && outcome.expired ? 'expired' : outcome.status
  await acts.bg_finish({ taskId, state: outcome.status === 'completed' ? 'done' : 'failed', detail })
}
