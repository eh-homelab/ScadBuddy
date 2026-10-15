import type { Client } from '@temporalio/client'
import { defaultPayloadConverter } from '@temporalio/common'
import { historyToJSON } from '@temporalio/common/lib/proto-utils.js'

// What a workflow was waiting on, for a test that failed or ran into its timeout (#3693): its
// status, its pending activities (attempt, last failure, heartbeat), its pending timers and the
// last events of its current run, as one block of text for the test's output.

type Payloads = { payloads?: { metadata?: Record<string, Uint8Array | null> | null; data?: Uint8Array | null }[] | null } | null | undefined
type JsonEvent = { eventId?: string; eventTime?: string; eventType?: string } & Record<string, unknown>

const decode = (payloads: Payloads): unknown => {
  const first = payloads?.payloads?.[0]
  if (!first) return undefined
  try {
    return defaultPayloadConverter.fromPayload(first as Parameters<typeof defaultPayloadConverter.fromPayload>[0])
  } catch (err) {
    return `<undecodable: ${(err as Error).message}>`
  }
}

const time = (t: { seconds?: unknown; nanos?: number | null } | null | undefined) =>
  t?.seconds !== undefined && t?.seconds !== null ? new Date(Number(t.seconds) * 1000 + Math.floor((t.nanos ?? 0) / 1e6)).toISOString() : undefined

/** The few attributes of an event worth a line: the activity, its queue, its failure. */
function brief(e: JsonEvent): string {
  const attrs = Object.entries(e).find(([k]) => k.endsWith('EventAttributes'))?.[1] as Record<string, unknown> | undefined
  if (!attrs) return ''
  const bits: string[] = []
  const at = attrs as {
    activityType?: { name?: string }
    taskQueue?: { name?: string }
    activityId?: string
    scheduledEventId?: string
    timerId?: string
    startToFireTimeout?: string
    failure?: { message?: string; cause?: { message?: string }; timeoutFailureInfo?: { timeoutType?: string } }
    identity?: string
    attempt?: number
    updateId?: string
    signalName?: string
  }
  if (at.activityType?.name) bits.push(at.activityType.name)
  if (at.activityId) bits.push(`id=${at.activityId}`)
  if (at.taskQueue?.name) bits.push(`queue=${at.taskQueue.name}`)
  if (at.scheduledEventId) bits.push(`scheduled=${at.scheduledEventId}`)
  if (at.timerId) bits.push(`timer=${at.timerId}${at.startToFireTimeout ? ` ${at.startToFireTimeout}` : ''}`)
  if (at.identity) bits.push(`by=${at.identity}`)
  if (at.attempt) bits.push(`attempt=${at.attempt}`)
  if (at.signalName) bits.push(`signal=${at.signalName}`)
  if (at.failure) bits.push(`failure=${at.failure.timeoutFailureInfo?.timeoutType ?? ''} ${at.failure.message ?? ''}${at.failure.cause?.message ? ` <- ${at.failure.cause.message}` : ''}`.trim())
  return bits.join(' ')
}

/** The workflow's state as text: status, pending activities and timers, the last `last` events of its current run. */
export async function dumpWorkflow(client: Client, workflowId: string, last = 30): Promise<string> {
  const lines: string[] = [`--- workflow ${workflowId}`]
  try {
    const handle = client.workflow.getHandle(workflowId)
    const d = await handle.describe()
    lines.push(`status=${d.status.name} run=${d.runId} historyLength=${d.historyLength} start=${d.startTime.toISOString()} now=${new Date().toISOString()}`)
    for (const a of d.raw.pendingActivities ?? []) {
      lines.push(
        `pending activity ${a.activityType?.name} id=${a.activityId} state=${a.state} attempt=${a.attempt}/${a.maximumAttempts}` +
          ` scheduled=${time(a.scheduledTime)} lastStarted=${time(a.lastStartedTime)} lastHeartbeat=${time(a.lastHeartbeatTime)}` +
          ` expiration=${time(a.expirationTime)} worker=${a.lastWorkerIdentity ?? ''}` +
          ` heartbeat=${JSON.stringify(decode(a.heartbeatDetails as Payloads))}` +
          ` lastFailure=${a.lastFailure ? JSON.stringify({ message: a.lastFailure.message, cause: a.lastFailure.cause?.message }) : 'none'}`,
      )
    }
    if (!d.raw.pendingActivities?.length) lines.push('pending activities: none')
    const events = (JSON.parse(historyToJSON(await handle.fetchHistory())) as { events: JsonEvent[] }).events
    const started = new Map<string, JsonEvent>()
    for (const e of events) {
      const timer = (e.timerStartedEventAttributes as { timerId?: string } | undefined)?.timerId
      if (e.eventType === 'EVENT_TYPE_TIMER_STARTED' && timer) started.set(timer, e)
      const closed =
        (e.timerFiredEventAttributes as { timerId?: string } | undefined)?.timerId ?? (e.timerCanceledEventAttributes as { timerId?: string } | undefined)?.timerId
      if (closed) started.delete(closed)
    }
    lines.push(started.size ? `pending timers: ${[...started.values()].map((e) => `#${e.eventId} ${brief(e)} at ${e.eventTime}`).join('; ')}` : 'pending timers: none')
    lines.push(`last ${Math.min(last, events.length)} of ${events.length} events:`)
    for (const e of events.slice(-last)) lines.push(`  #${e.eventId} ${e.eventTime} ${String(e.eventType).replace('EVENT_TYPE_', '')} ${brief(e)}`)
  } catch (err) {
    lines.push(`could not describe it: ${(err as Error).message}`)
  }
  return lines.join('\n')
}
