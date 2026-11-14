import { useEffect, useRef } from 'react'

/**
 * #815 — the agent's event stream that says, with no data, when what
 * `GET /api/v1/ai/pending-input` lists may have changed (agent routes/pendingInput.ts
 * `PENDING_CHANGES_PATH`). The agent sends it for every write of a call parked on a person
 * (`ai_pending_input`, `ai_approvals`, `ai_questions`), and once when the stream opens.
 */
export const PENDING_CHANGES_PATH = '/api/v1/ai/pending-input/changes'

/** #265 review: the first wait before a stream that closed for good is opened again. */
export const REOPEN_MS = 15_000
/** The longest wait between tries: each try that closes again doubles the wait. */
export const REOPEN_MAX_MS = 5 * 60_000

type Listener = () => void

const listeners = new Set<Listener>()
let source: EventSource | undefined
let closed = false
let reopenMs = REOPEN_MS
let reopenTimer: ReturnType<typeof setTimeout> | undefined
/** `EventSource.CLOSED`. */
const CLOSED = 2

function onChanged() {
  for (const listener of [...listeners]) listener()
}

function onOpen() {
  closed = false
  reopenMs = REOPEN_MS
}

function onError() {
  // A lost connection reconnects by itself, and the stream's first tick then reads
  // again. A 204 or a 503 (an agent without the stream, or one that restarts) closes it
  // for good: it is opened again after a back-off, so the views leave their timer once
  // the agent answers again. A stream that opens sends its first tick, which reads.
  if (source?.readyState !== CLOSED) return
  closed = true
  drop()
  reopenTimer = setTimeout(() => {
    reopenTimer = undefined
    if (listeners.size > 0) open()
  }, reopenMs)
  reopenMs = Math.min(reopenMs * 2, REOPEN_MAX_MS)
}

function open() {
  if (source || typeof EventSource === 'undefined') return
  source = new EventSource(PENDING_CHANGES_PATH)
  source.addEventListener('changed', onChanged)
  source.addEventListener('open', onOpen)
  source.addEventListener('error', onError)
}

function drop() {
  if (!source) return
  source.removeEventListener('changed', onChanged)
  source.removeEventListener('open', onOpen)
  source.removeEventListener('error', onError)
  source.close()
  source = undefined
}

/**
 * #265 — follows the stream, one `EventSource` per tab however many follow it: the
 * header's badge (`agent/attention.ts`) and each view of a flow run's queue. The
 * returned function stops following; the last one closes the stream.
 */
export function subscribePendingChanges(listener: Listener): () => void {
  listeners.add(listener)
  // While a back-off waits, its timer opens the stream.
  if (reopenTimer === undefined) open()
  return () => {
    if (!listeners.delete(listener) || listeners.size > 0) return
    drop()
    clearTimeout(reopenTimer)
    reopenTimer = undefined
    closed = false
    reopenMs = REOPEN_MS
  }
}

/**
 * Whether the stream cannot tell of changes: it closed for good and has not opened
 * again since, or there is no `EventSource`.
 */
export function pendingChangesUnavailable(): boolean {
  return closed || typeof EventSource === 'undefined'
}

/**
 * #265 — calls `read` on each change the agent's stream announces while `active`: for
 * data that only the agent knows has changed, such as a child session's call decided in
 * its own session (#2482: a flow run's queue). It calls `read` every `ms` only while the
 * stream is unavailable (`pendingChangesUnavailable`), as the realtime topics poll only
 * while their socket is (`usePollWhileUnavailable`).
 *
 * Accepted exception to the realtime socket (#265): the pending input lives in the agent
 * service, and the backend's socket (`WS /api/v1/ws`) cannot carry it without a new
 * cross-service path, which the database rule (#5295) forbids. The agent's own stream
 * carries it instead.
 */
export function useReadOnPendingChanges(active: boolean, read: () => void, ms: number): void {
  const latest = useRef(read)
  useEffect(() => {
    latest.current = read
  })
  useEffect(() => {
    if (!active) return
    const unfollow = subscribePendingChanges(() => latest.current())
    const timer = setInterval(() => {
      if (pendingChangesUnavailable()) latest.current()
    }, ms)
    return () => {
      unfollow()
      clearInterval(timer)
    }
  }, [active, ms])
}
