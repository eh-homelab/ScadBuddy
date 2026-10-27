import { createContext, useCallback, useEffect, useRef, useState } from 'react'
import { z } from 'zod'
import { isDoneSummary, PendingInputSchema } from './chat/protocol'

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
 * are #815's later parts. Durable sessions' entries are in the same read; one past its
 * timer whose worker has not resolved it yet is `expiring` and not counted.
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
  /**
   * Whether more summaries are pending than the agent listed (its `summaries_truncated`), so
   * `summaries` is a lower bound; undefined when the agent does not say (an older agent).
   */
  summariesTruncated?: boolean
  /** #2482 — the browser-started runs whose own calls wait for you (`RunsWaitingContext`). */
  runs?: RunWaiting[]
  /** #4813 — the approvals in no session and no flow run, which no feed shows (`ApprovalsWaitingContext`). */
  unattached?: PendingApproval[]
}

/** What waits on the user: everything but the done summaries. */
export const totalOf = (c: PendingCounts): number => c.approvals + c.questions + c.attention

/** What the agent lists as parked on the user, or null when it did not answer as the agent. Never throws, and settles within `timeoutMs`. */
export function fetchPendingInput(timeoutMs = ATTENTION_TIMEOUT_MS): Promise<PendingCounts | null> {
  return withDeadline(read, timeoutMs)
}

/**
 * #1125 — the user's sessions with a turn under way: `GET /api/v1/ai/sessions?status=running`
 * (agent routes/sessions.ts), every origin the browser user can see, so a background or MCP
 * session counts as one this tab's socket never hears of. One row says yes.
 *
 * Only `running` on purpose, not `durable.ts`'s `RUNNING` set: a turn parked in
 * `waiting_approval` or `waiting_input` is waiting on the user, which the waiting badge
 * already shows, so the "working" dot is off for it.
 */
export const RUNNING_PATH = '/api/v1/ai/sessions?status=running&limit=1'
const RunningSchema = z.object({ sessions: z.array(z.unknown()) })

/** Whether any of the user's sessions is running, or null when the agent did not answer. Never throws, and settles within `timeoutMs`. */
export function fetchRunning(timeoutMs = ATTENTION_TIMEOUT_MS): Promise<boolean | null> {
  return withDeadline(readRunning, timeoutMs)
}

async function readRunning(signal: AbortSignal): Promise<boolean | null> {
  try {
    const response = await fetch(RUNNING_PATH, { headers: { Accept: 'application/json' }, cache: 'no-store', signal })
    if (!response.ok || !(response.headers.get('content-type') ?? '').includes('application/json')) return null
    const parsed = RunningSchema.safeParse(await response.json())
    return parsed.success ? parsed.data.sessions.length > 0 : null
  } catch {
    return null
  }
}

async function withDeadline<T>(run: (signal: AbortSignal) => Promise<T | null>, timeoutMs: number): Promise<T | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new DOMException('timed out', 'TimeoutError')), timeoutMs)
  // Raced as well as aborted, as `fetchAiAvailability` does: a fetch that ignores the
  // signal must not hold the poll (`useAttention` starts no read while one is open).
  const deadline = new Promise<null>((resolve) => {
    controller.signal.addEventListener('abort', () => resolve(null), { once: true })
  })
  try {
    return await Promise.race([run(controller.signal), deadline])
  } finally {
    clearTimeout(timer)
  }
}

/** #2482 — a flow run with calls waiting for the person, and how many: what the badge counts for it. */
export interface RunWaiting {
  id: string
  name: string
  waiting: number
}

/**
 * #2482 — the flow runs started from the browser whose own calls wait for you, from the
 * badge's one read: the panel lists them (`RunsWaiting`), since such a run has no session
 * to list them under. A run a session started is listed under that session's runs
 * (`SessionFlowRuns`). Null outside the shell, or before the agent answered.
 */
export const RunsWaitingContext = createContext<readonly RunWaiting[] | null>(null)

/** The browser-started runs with calls waiting for you, as `read` counts them. */
function runsWaiting(entries: readonly PendingEntry[]): RunWaiting[] {
  const runs = new Map<string, RunWaiting>()
  for (const entry of entries) {
    const run = entry.run
    if (entry.expiring || !run?.id || run.hop === 'parent' || run.starter_session) continue
    const seen = runs.get(run.id) ?? { id: run.id, name: run.name ?? run.id, waiting: 0 }
    seen.waiting += 1
    runs.set(run.id, seen)
  }
  return [...runs.values()]
}

type PendingEntry = z.infer<typeof PendingInputSchema>['entries'][number]

/**
 * #4813 — an approval prepared outside any session (an external MCP client's `pending_approval`,
 * AI spec §8.2): an `ai_approvals` row with no session, so no chat feed has a card for it. The
 * badge counts it; `ApprovalsWaiting` and the shell's toast are where it is decided.
 */
export interface PendingApproval {
  /** The pending-input request id, `approval:<id>`, that `respond` takes. */
  id: string
  tool: string
  summary: string
  /** Who prepared it (an MCP token's label), when the agent says. */
  by?: string
  expiresAt?: string
}

/** The badge's own read, and a way to read it again once one is decided; null outside the shell. */
export const ApprovalsWaitingContext = createContext<{ approvals: readonly PendingApproval[]; refresh: () => void } | null>(null)

/** The approvals in no session and no flow run, as `read` counts them. */
function unattached(entries: readonly PendingEntry[]): PendingApproval[] {
  const approvals: PendingApproval[] = []
  for (const entry of entries) {
    if (entry.kind !== 'approval' || entry.expiring || entry.run || entry.session_id !== null || !entry.id) continue
    approvals.push({
      id: entry.id,
      tool: entry.tool ?? '',
      summary: entry.summary ?? '',
      ...(entry.requested_by ? { by: entry.requested_by.label } : {}),
      ...(entry.expires_at ? { expiresAt: entry.expires_at } : {}),
    })
  }
  return approvals
}

async function read(signal: AbortSignal): Promise<PendingCounts | null> {
  try {
    const response = await fetch(ATTENTION_PATH, { headers: { Accept: 'application/json' }, cache: 'no-store', signal })
    if (!response.ok || !(response.headers.get('content-type') ?? '').includes('application/json')) return null
    const parsed = PendingInputSchema.safeParse(await response.json())
    if (!parsed.success) return null
    const counts: PendingCounts = { approvals: 0, questions: 0, attention: 0, summaries: 0 }
    for (const entry of parsed.data.entries) {
      if (entry.expiring) continue
      // #2482: a flow run's call still with the session that started it (spec 2026-10-11 §4.5).
      if (entry.run?.hop === 'parent') continue
      // An approval is the call's decision; an answer is a question, or an attention request when it says so
      // (a summary when its reason is `done`).
      if (entry.kind === 'approval') counts.approvals += 1
      else if (entry.attention === undefined) counts.questions += 1
      else if (isDoneSummary(entry.attention.reason, entry.attention.on_timeout !== null)) counts.summaries += 1
      else counts.attention += 1
    }
    if (parsed.data.summaries_truncated !== undefined) counts.summariesTruncated = parsed.data.summaries_truncated
    const runs = runsWaiting(parsed.data.entries)
    if (runs.length > 0) counts.runs = runs
    const loose = unattached(parsed.data.entries)
    if (loose.length > 0) counts.unattached = loose
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
  /**
   * #1125 — whether one of the user's sessions has a turn under way: the last known
   * answer when a read fails, and null until one has succeeded.
   */
  running: boolean | null
  /** Reads again now (the panel was toggled, so a decision may just have landed). */
  refresh: () => void
}

/** Polls what is waiting on the user while `enabled`, and again whenever the tab comes back into view. */
export function useAttention(enabled: boolean): Attention {
  const [counts, setCounts] = useState<PendingCounts | null>(null)
  const [running, setRunning] = useState<boolean | null>(null)
  const generation = useRef(0)
  // One read at a time: while the agent is slow to answer, the timer, focus and
  // toggles must not pile requests up behind it. Held per generation, so a read
  // left open by a switch off does not hold back the first read after switching on.
  const inflight = useRef<number | null>(null)

  const refresh = useCallback(() => {
    if (!enabled || inflight.current === generation.current) return
    const started = generation.current
    inflight.current = started
    // Each answer is shown as it lands, so a slow sessions list does not hold back the
    // waiting count, or the reverse. A failed read keeps the last answer: an outage is
    // not "nothing is waiting", or "idle". The slot frees once both have settled.
    const current = () => started === generation.current
    void Promise.all([
      fetchPendingInput().then((c) => {
        if (current() && c !== null) setCounts(c)
      }),
      fetchRunning().then((r) => {
        if (current() && r !== null) setRunning(r)
      }),
    ]).then(() => {
      if (inflight.current === started) inflight.current = null
    })
  }, [enabled])

  useEffect(() => {
    if (!enabled) {
      generation.current += 1
      setCounts(null)
      setRunning(null)
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

  return { waiting: counts === null ? null : totalOf(counts), counts, running, refresh }
}

/** The most rows of one kind the agent lists (agent `approvals/service.ts` `list`, `questions/service.ts` `listPending`, `LIMIT 500`). */
export const APPROVALS_LIST_MAX = 500

/** #1125 — what the header says while a session's turn is under way. */
export const WORKING_LABEL = 'working'

/** The count as shown: past a full page from the agent, `500+`. */
export function attentionCount(n: number): string {
  return n >= APPROVALS_LIST_MAX ? `${APPROVALS_LIST_MAX}+` : String(n)
}

/** What the header says about `n` things waiting; empty for none or unknown. */
export function attentionLabel(n: number | null): string {
  if (n === null || n <= 0) return ''
  return `${attentionCount(n)} waiting for you`
}

/**
 * The done summaries, shown beside the waiting count but not in it ("1 summary"); empty for none or
 * unknown. `500+` when the agent says it listed only some of them, so older ones cut from the list
 * are not silently uncounted; an agent that does not say gets the full-page guess (`attentionCount`).
 */
export function summaryLabel(c: PendingCounts | null): string {
  if (c === null || c.summaries <= 0) return ''
  const n =
    c.summariesTruncated === undefined ? attentionCount(c.summaries) : c.summariesTruncated ? `${c.summaries}+` : String(c.summaries)
  return `${n} ${c.summaries === 1 && !c.summariesTruncated ? 'summary' : 'summaries'}`
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
 * The tab title: the page's own `title` (#2454, `usePageTitle`), with the waiting count
 * in front of it (`(2) Library · ScadBuddy`), so a background tab shows it in the tab
 * strip. Never prefixed when off, as embedded: inside Bambuddy's frame the tab shows
 * Bambuddy's title, and the frame's own is seen by nobody. On unmount it leaves the
 * last title it set, deliberately: AppShell, the root, is its only caller, and the next
 * page's title replaces it.
 */
export function useAttentionTitle(waiting: number | null, enabled: boolean, title: string): void {
  useEffect(() => {
    document.title = enabled && waiting !== null && waiting > 0 ? `(${attentionCount(waiting)}) ${title}` : title
  }, [waiting, enabled, title])
}
