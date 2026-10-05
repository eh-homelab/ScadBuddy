import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * #815 — whether the assistant is waiting on the user, for the header outside the
 * panel. The panel's own "needs your approval" status is unmounted with the panel,
 * and a call parked by a background or MCP session never reaches this tab's socket
 * at all, so the shell asks the agent directly: `GET /api/v1/ai/pending-input` lists
 * every tool call parked on the browser user, of every kind (durable-agents spec
 * §6.6, agent/src/routes/pendingInput.ts): approvals of outward calls, the agent's
 * questions, and its attention requests (`request_user_attention`). So the badge
 * means "things waiting for you", not only approvals.
 *
 * This is the in-app channel only. OS notifications and the tab-disconnected trigger
 * are #815's later parts. Durable sessions join the same read when they land.
 */

export const ATTENTION_PATH = '/api/v1/ai/pending-input'
/** How often the count is read while the assistant is available. */
export const ATTENTION_POLL_MS = 15_000

/** How long one read may take; a proxy that accepts and never answers counts as no answer. */
export const ATTENTION_TIMEOUT_MS = 8_000

/** What is waiting on the user, by kind, and the `done` summaries beside them. */
export interface PendingCounts {
  approvals: number
  questions: number
  attention: number
  /** `done` attention requests: a turn's summary, which waits for nothing, so it is not in `totalOf`. */
  summaries: number
}

/** What waits on the user: everything but the done summaries. */
export const totalOf = (c: PendingCounts): number => c.approvals + c.questions + c.attention

/** What the agent lists as parked on the user, or null when it did not answer as the agent. Never throws, and settles within `timeoutMs`. */
export async function fetchPendingInput(timeoutMs = ATTENTION_TIMEOUT_MS): Promise<PendingCounts | null> {
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

const isDone = (attention: unknown): boolean =>
  typeof attention === 'object' && attention !== null && (attention as { reason?: unknown }).reason === 'done'

async function read(signal: AbortSignal): Promise<PendingCounts | null> {
  try {
    const response = await fetch(ATTENTION_PATH, { headers: { Accept: 'application/json' }, cache: 'no-store', signal })
    if (!response.ok || !(response.headers.get('content-type') ?? '').includes('application/json')) return null
    const body: unknown = await response.json()
    if (typeof body !== 'object' || body === null) return null
    const entries = (body as { entries?: unknown }).entries
    if (!Array.isArray(entries)) return null
    const counts: PendingCounts = { approvals: 0, questions: 0, attention: 0, summaries: 0 }
    for (const e of entries as unknown[]) {
      if (typeof e !== 'object' || e === null) continue
      const entry = e as { kind?: unknown; attention?: unknown }
      // An approval is the call's decision; an answer is a question, or an attention request when it says so
      // (a summary when its reason is `done`).
      if (entry.kind === 'approval') counts.approvals += 1
      else if (isDone(entry.attention)) counts.summaries += 1
      else if (entry.attention !== undefined) counts.attention += 1
      else counts.questions += 1
    }
    return counts
  } catch {
    return null
  }
}

export interface Attention {
  /**
   * Everything waiting on the user: the last known count when a read fails, and null
   * until one has succeeded (unknown is not "nothing is waiting").
   */
  waiting: number | null
  /** The same, by kind; null exactly when `waiting` is. */
  counts: PendingCounts | null
  /** Reads again now (the panel was toggled, so a decision may just have landed). */
  refresh: () => void
}

/** Polls what is waiting on the user while `enabled`, and again whenever the tab comes back into view. */
export function useAttention(enabled: boolean): Attention {
  const [counts, setCounts] = useState<PendingCounts | null>(null)
  const generation = useRef(0)
  // One read at a time: while the agent is slow to answer, the timer, focus and
  // toggles must not pile requests up behind it. Held per generation, so a read
  // left open by a switch off does not hold back the first read after switching on.
  const inflight = useRef<number | null>(null)

  const refresh = useCallback(() => {
    if (!enabled || inflight.current === generation.current) return
    const started = generation.current
    inflight.current = started
    void fetchPendingInput().then((c) => {
      if (inflight.current === started) inflight.current = null
      // A failed read keeps the last count: an outage is not "nothing is waiting".
      if (c !== null && started === generation.current) setCounts(c)
    })
  }, [enabled])

  useEffect(() => {
    if (!enabled) {
      generation.current += 1
      setCounts(null)
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

  return { waiting: counts === null ? null : totalOf(counts), counts, refresh }
}

/** The most rows of one kind the agent lists (agent `approvals/service.ts` `list`, `questions/service.ts` `listPending`, `LIMIT 500`). */
export const APPROVALS_LIST_MAX = 500

/** The count as shown: past a full page from the agent, `500+`. */
export function attentionCount(n: number): string {
  return n >= APPROVALS_LIST_MAX ? `${APPROVALS_LIST_MAX}+` : String(n)
}

/** What the header says about `n` things waiting; empty for none or unknown. */
export function attentionLabel(n: number | null): string {
  if (n === null || n <= 0) return ''
  return `${attentionCount(n)} waiting for you`
}

/** The done summaries, shown beside the waiting count but not in it ("1 summary"); empty for none or unknown. */
export function summaryLabel(c: PendingCounts | null): string {
  if (c === null || c.summaries <= 0) return ''
  return `${attentionCount(c.summaries)} ${c.summaries === 1 ? 'summary' : 'summaries'}`
}

/** The counts by kind, for the toggle's title ("2 approvals, 1 question"); empty for none or unknown. */
export function attentionDetail(c: PendingCounts | null): string {
  if (c === null) return ''
  const part = (n: number, one: string, many: string) => (n > 0 ? [`${attentionCount(n)} ${n === 1 ? one : many}`] : [])
  return [
    ...part(c.approvals, 'approval', 'approvals'),
    ...part(c.questions, 'question', 'questions'),
    ...part(c.attention, 'attention request', 'attention requests'),
  ].join(', ')
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
      document.title = `(${attentionCount(waiting)}) ${bare.current}`
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
