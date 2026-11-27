import { useCallback, useEffect, useRef, useState } from 'react'
import {
  callTopic,
  DeviceOfflineError,
  ENDING_KINDS,
  getCall,
  startCall,
  type CallEvent,
  type CallOutcome,
  type CallResult,
  type CallView,
  type DeviceMethod,
} from '../../api/devices'
import { useRealtimeStatus, useSubscription } from '../../lib/realtime'
import { eventText, FAILED, merged, progressOf, PROMPT, str } from './callText'
import { Spinner } from '../ui/spinner'
import { cn } from '@/lib/utils'

/** How the call ended, for the parent. `read_id` is set when the device stored a read. */
export interface CallEnd {
  outcome: CallOutcome
  result: CallResult | null
  error?: string
}

/** How often the call is read while the realtime socket is down. */
export const POLL_MS = 2_000
/** How long after the call's deadline the page reads it once more, in case its end was missed. */
const DEADLINE_SLACK_MS = 5_000
/** Past the deadline, how often the call is read until core ends it. */
export const LATE_POLL_MS = 10_000
/** The longest delay `setTimeout` keeps; a longer one fires at once. */
const MAX_TIMER_MS = 2 ** 31 - 1

type Phase = { kind: 'starting' } | { kind: 'open'; callId: string; deadline?: string } | { kind: 'ended'; end: CallEnd } | { kind: 'refused'; message: string }

/**
 * #7905 — one call to a paired device (plan Task 9). It starts the call on mount and shows its
 * events as the realtime socket gives them (topic `device:call:<id>`). Each `'resync'` reads the
 * call (`GET /devices/calls/{id}`), whose events come in log order, so an event sent before the
 * subscription was live still shows. While the socket is down it reads every `POLL_MS`, and past
 * the deadline every `LATE_POLL_MS` until core ends the call. A signal that comes while a read is
 * out reads once more after it, so the read that shows the end is never dropped. When the
 * call ends, `onEnd` gets the outcome and the result (its `read_id`). Mount it again, with a new
 * `key`, to call again.
 */
export function DeviceCall({
  deviceId,
  method,
  args,
  timeoutS = 60,
  onEnd,
}: {
  deviceId: string
  method: DeviceMethod
  args?: Record<string, unknown>
  timeoutS?: number
  onEnd?: (end: CallEnd) => void
}) {
  const [phase, setPhase] = useState<Phase>({ kind: 'starting' })
  const [events, setEvents] = useState<CallEvent[]>([])
  const started = useRef(false)
  const ended = useRef(false)
  const latestOnEnd = useRef(onEnd)
  useEffect(() => {
    latestOnEnd.current = onEnd
  })

  const finish = useCallback((end: CallEnd) => {
    if (ended.current) return
    ended.current = true
    setPhase({ kind: 'ended', end })
    latestOnEnd.current?.(end)
  }, [])

  useEffect(() => {
    // Once per mount, StrictMode's second run included: a second run would start a second call.
    if (started.current) return
    started.current = true
    startCall(deviceId, method, args, timeoutS).then(
      ({ call_id }) => setPhase((now) => (now.kind === 'starting' ? { kind: 'open', callId: call_id } : now)),
      (error: unknown) => {
        ended.current = true
        const offline = error instanceof DeviceOfflineError
        const message = offline ? error.message : `The call could not start: ${error instanceof Error ? error.message : String(error)}`
        setPhase({ kind: 'refused', message })
        latestOnEnd.current?.({ outcome: offline ? 'offline' : 'error', result: null, error: message })
      },
    )
  }, [deviceId, method, args, timeoutS])

  const callId = phase.kind === 'open' ? phase.callId : undefined
  const reading = useRef(false)
  const again = useRef(false)
  const read = useCallback(() => {
    if (!callId || ended.current) return
    if (reading.current) {
      again.current = true
      return
    }
    const run = () => {
      reading.current = true
      again.current = false
      getCall(callId)
        .then((view: CallView) => {
          setEvents((current) => merged(current, view.events ?? []))
          setPhase((now) => (now.kind === 'open' && now.callId === callId && !now.deadline ? { ...now, deadline: view.call.deadline } : now))
          if (view.call.outcome) finish({ outcome: view.call.outcome, result: view.result })
        })
        // A failed read leaves the call open: the next signal, poll or the deadline reads again.
        .catch(() => {})
        .finally(() => {
          reading.current = false
          if (again.current && !ended.current) run()
        })
    }
    run()
  }, [callId, finish])

  useSubscription(callId ? callTopic(callId) : undefined, (signal) => {
    if (signal === 'resync') {
      read()
      return
    }
    setEvents((current) => (current.some((one) => one.id === signal.id) ? current : [...current, { id: signal.id, kind: signal.kind, data: signal.data }]))
    if (!ENDING_KINDS.has(signal.kind)) return
    if (signal.kind === 'device.call_done') {
      const { call: _call, device: _device, ...result } = signal.data
      finish({ outcome: 'done', result: result as CallResult })
    } else {
      finish({ outcome: (str(signal.data.outcome) as CallOutcome | undefined) ?? 'error', result: null, error: str(signal.data.error) })
    }
  })

  const { status } = useRealtimeStatus()
  useEffect(() => {
    if (!callId || status !== 'unavailable') return
    const timer = setInterval(read, POLL_MS)
    return () => clearInterval(timer)
  }, [callId, status, read])

  const deadline = phase.kind === 'open' ? phase.deadline : undefined
  useEffect(() => {
    if (!callId) return
    const at = deadline ? new Date(deadline).getTime() : Date.now() + timeoutS * 1000
    let late: ReturnType<typeof setInterval> | undefined
    const timer = setTimeout(() => {
      read()
      late = setInterval(read, LATE_POLL_MS)
    }, Math.min(Math.max(0, at - Date.now()) + DEADLINE_SLACK_MS, MAX_TIMER_MS))
    return () => {
      clearTimeout(timer)
      clearInterval(late)
    }
  }, [callId, deadline, timeoutS, read])

  const progress = method === 'nfc.read' ? progressOf(events) : undefined
  const lost = events.at(-1)?.kind === 'device.tag_lost'

  return (
    <div className="flex flex-col gap-3" data-testid="device-call">
      {phase.kind === 'refused' ? (
        <p role="alert" className="text-warn">
          {phase.message}
        </p>
      ) : (
        <p className="flex items-center gap-2 font-medium">
          {phase.kind !== 'ended' && <Spinner />}
          {phase.kind === 'starting' && 'Asking the phone…'}
          {phase.kind === 'open' && PROMPT[method]}
          {phase.kind === 'ended' && (phase.end.outcome === 'done' ? 'Finished.' : 'Stopped.')}
        </p>
      )}
      {progress && <BlockStrip read={progress.read} of={progress.of} lost={lost} />}
      {events.length > 0 && (
        <ol aria-label="What the phone did" aria-live="polite" className="flex flex-col gap-1 border-l-2 border-line pl-3">
          {events.map((event) => (
            <li
              key={event.id}
              className={cn(
                event.kind === 'device.tag_lost' && 'text-warn',
                event.kind === 'device.call_failed' && 'text-destructive',
                event.kind === 'device.call_done' && 'text-ok',
              )}
            >
              {eventText(event)}
            </li>
          ))}
        </ol>
      )}
      {phase.kind === 'ended' && phase.end.outcome !== 'done' && !events.some((one) => one.kind === 'device.call_failed') && (
        <p role="alert" className="text-destructive">
          {FAILED[phase.end.outcome]}
          {phase.end.error ? ` ${phase.end.error}` : ''}
        </p>
      )}
    </div>
  )
}

/** One cell per tag block: the read ones filled. A lost tag shows where the read stopped. */
function BlockStrip({ read, of, lost }: { read: number; of: number; lost: boolean }) {
  const cells = Math.min(Math.max(of, 1), 64)
  const filled = Math.round((Math.min(read, of) / Math.max(of, 1)) * cells)
  return (
    <div
      role="progressbar"
      aria-label="Blocks read"
      aria-valuemin={0}
      aria-valuemax={of}
      aria-valuenow={Math.min(read, of)}
      aria-valuetext={`${Math.min(read, of)} of ${of} blocks`}
      className="grid gap-[2px]"
      style={{ gridTemplateColumns: `repeat(${Math.min(cells, 16)}, minmax(0, 1fr))` }}
    >
      {Array.from({ length: cells }, (_, at) => (
        <span
          key={at}
          className={cn(
            'h-2.5 rounded-[2px] transition-colors motion-reduce:transition-none',
            at < filled ? (lost ? 'bg-warn' : 'bg-primary') : 'bg-muted',
          )}
        />
      ))}
    </div>
  )
}
