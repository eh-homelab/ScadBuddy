import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * #815 — whether the assistant is waiting on the user, for the header outside the
 * panel. The panel's own "needs your approval" status is unmounted with the panel,
 * and an approval parked by a background or MCP session never reaches this tab's
 * socket at all, so the shell asks the agent directly: `GET /api/v1/ai/approvals`
 * with no session lists every pending approval the browser user may decide
 * (agent/src/routes/approvals.ts; expired ones are settled before it answers).
 *
 * This is the in-app channel only. OS notifications, the agent's own
 * `request_user_attention` tool and the tab-disconnected trigger are #815's later
 * parts. Durable sessions and flows (the durable-agents spec) keep their pending
 * approvals and `wait_for_human` in Temporal, not `ai_approvals`; when they land,
 * `fetchPendingApprovals` is the one place that also counts those.
 */

export const ATTENTION_PATH = '/api/v1/ai/approvals?pending=true'
/** How often the count is read while the assistant is available. */
export const ATTENTION_POLL_MS = 15_000

/** How long one read may take; a proxy that accepts and never answers counts as no answer. */
export const ATTENTION_TIMEOUT_MS = 8_000

/** The pending approvals the agent lists, or null when it did not answer as the agent. Never throws, and settles within `timeoutMs`. */
export async function fetchPendingApprovals(timeoutMs = ATTENTION_TIMEOUT_MS): Promise<number | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new DOMException('timed out', 'TimeoutError')), timeoutMs)
  // Raced as well as aborted, as `fetchAiAvailability` does: a fetch that ignores the
  // signal must not hold the poll (`useAttention` starts no read while one is open).
  const deadline = new Promise<null>((resolve) => {
    controller.signal.addEventListener('abort', () => resolve(null), { once: true })
  })
  try {
    return await Promise.race([read(controller.signal), deadline])
  } finally {
    clearTimeout(timer)
  }
}

async function read(signal: AbortSignal): Promise<number | null> {
  try {
    const response = await fetch(ATTENTION_PATH, { headers: { Accept: 'application/json' }, cache: 'no-store', signal })
    if (!response.ok || !(response.headers.get('content-type') ?? '').includes('application/json')) return null
    const body: unknown = await response.json()
    if (typeof body !== 'object' || body === null) return null
    const approvals = (body as { approvals?: unknown }).approvals
    return Array.isArray(approvals) ? approvals.length : null
  } catch {
    return null
  }
}

export interface Attention {
  /**
   * Approvals waiting on the user: the last known count when a read fails, and null
   * until one has succeeded (unknown is not "nothing is waiting").
   */
  waiting: number | null
  /** Reads again now (the panel was toggled, so a decision may just have landed). */
  refresh: () => void
}

/** Polls the pending approvals while `enabled`, and again whenever the tab comes back into view. */
export function useAttention(enabled: boolean): Attention {
  const [waiting, setWaiting] = useState<number | null>(null)
  const generation = useRef(0)
  // One read at a time: while the agent is slow to answer, the timer, focus and
  // toggles must not pile requests up behind it.
  const inflight = useRef(false)

  const refresh = useCallback(() => {
    if (!enabled || inflight.current) return
    inflight.current = true
    const started = generation.current
    void fetchPendingApprovals().then((n) => {
      inflight.current = false
      // A failed read keeps the last count: an outage is not "nothing is waiting".
      if (n !== null && started === generation.current) setWaiting(n)
    })
  }, [enabled])

  useEffect(() => {
    if (!enabled) {
      generation.current += 1
      setWaiting(null)
      return
    }
    refresh()
    const timer = setInterval(refresh, ATTENTION_POLL_MS)
    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', refresh)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', refresh)
    }
  }, [enabled, refresh])

  return { waiting, refresh }
}

/** What the header says about `n` waiting approvals; empty for none or unknown. */
export function attentionLabel(n: number | null): string {
  if (n === null || n <= 0) return ''
  return `${n} ${n === 1 ? 'action' : 'actions'} waiting for your approval`
}

/**
 * Puts the waiting count in front of the tab title (`(2) ScadBuddy`), so a background
 * tab shows it in the tab strip, and restores the title it found at zero. The title is
 * remembered rather than parsed back, so a page title that happens to start with
 * `(n) ` is never mistaken for this prefix. Off when embedded: inside Bambuddy's frame
 * the tab shows Bambuddy's title, and the frame's own is seen by nobody.
 */
export function useAttentionTitle(waiting: number | null, enabled: boolean): void {
  const bare = useRef<string | null>(null)
  useEffect(() => {
    if (enabled && waiting !== null && waiting > 0) {
      bare.current ??= document.title
      document.title = `(${waiting}) ${bare.current}`
    } else if (bare.current !== null) {
      document.title = bare.current
      bare.current = null
    }
  }, [waiting, enabled])
  useEffect(
    () => () => {
      if (bare.current !== null) document.title = bare.current
    },
    [],
  )
}
