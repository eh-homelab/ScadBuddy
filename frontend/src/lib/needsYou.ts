import { createContext, useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { FlowGrantRequests, FlowRunGrantRequest, FlowTrigger } from '../api/types'
import { ATTENTION_POLL_MS, type AttentionNotice, type WaitingEntry } from '../agent/attention'
import type { PairingEntry } from '../agent/linkProtocol'
import { noticeTitle } from '../agent/attentionNotifications'
import { getRealtime } from './realtime'
import { dismissToast, toast } from './toast'

/**
 * #6399 — every request that waits on the person, of every kind, in one list: what the bell's
 * "Needs you" and the Activity page's section show, and what the bell counts.
 *
 * - the agent's pending input (`attention.ts` `WaitingEntry`): approvals in a session, outward
 *   actions an MCP client prepared (`confirm_action`), questions, attention requests, and a flow
 *   run's calls at hop `person`;
 * - the flow grants a person has to decide (`GET /api/v1/flow-grant-requests`): a run's
 *   requested grant, and a trigger's requested standing grant;
 * - the browser pairing requests this tab's link heard (an MCP client asks to drive a tab).
 *
 * Who sees what is the routes' own rule: the agent lists to the browser user what it lists, and
 * the grant requests are read as any flow read is. Only a person decides each one.
 */
export type NeedsYouItem =
  | { kind: 'agent'; key: string; since?: string; entry: WaitingEntry }
  | { kind: 'run-grant'; key: string; since?: string; request: FlowRunGrantRequest }
  | { kind: 'trigger-grant'; key: string; since?: string; trigger: FlowTrigger }
  | { kind: 'pairing'; key: string; since?: string; pairing: PairingEntry }

/** The Activity page's section, which the bell's "See all" and a notification open. */
export const NEEDS_YOU_ID = 'needs-you'
export const NEEDS_YOU_PATH = `/activity#${NEEDS_YOU_ID}`

/** The query parameter that opens one item's review on Activity (the bell's trigger "Review"). */
export const REVIEW_PARAM = 'review'

/** Activity's "Needs you" with `key`'s review open. */
export const reviewPath = (key: string) => `/activity?${REVIEW_PARAM}=${encodeURIComponent(key)}#${NEEDS_YOU_ID}`

/** Every item, the oldest first; an item with no time of its own (a pairing) last. */
export function needsYouItems(
  waiting: readonly WaitingEntry[],
  grants: FlowGrantRequests | null,
  pairings: readonly PairingEntry[],
): NeedsYouItem[] {
  const items: NeedsYouItem[] = [
    ...waiting.map((entry, i): NeedsYouItem => ({
      kind: 'agent',
      key: `agent:${entry.id || i}`,
      ...(entry.since ? { since: entry.since } : {}),
      entry,
    })),
    ...(grants?.runs ?? []).map(
      (request): NeedsYouItem => ({ kind: 'run-grant', key: `run-grant:${request.grant.id}`, since: request.grant.created_at, request }),
    ),
    ...(grants?.triggers ?? []).map(
      (trigger): NeedsYouItem => ({
        kind: 'trigger-grant',
        // An edit returns an approved grant to requested in place: its `requested_at` makes it
        // a new request, with the id the agent's push names it by (`crud_agent_notify.py`).
        key: trigger.grant ? `trigger-grant:${trigger.grant.id}:${Math.floor(Date.parse(trigger.grant.requested_at) / 1000)}` : `trigger-grant:${trigger.id}`,
        ...(trigger.grant ? { since: trigger.grant.requested_at } : {}),
        trigger,
      }),
    ),
    ...pairings.map((pairing): NeedsYouItem => ({ kind: 'pairing', key: `pairing:${pairing.id}`, pairing })),
  ]
  const at = (item: NeedsYouItem) => (item.since ? Date.parse(item.since) : Number.POSITIVE_INFINITY)
  return items.sort((a, b) => at(a) - at(b))
}

/** What the bell's and the tab title's count say: "2 need you"; empty for none. */
export function needsYouLabel(n: number): string {
  return n > 0 ? `${n} ${n === 1 ? 'needs' : 'need'} you` : ''
}

/** The OS notification each item that is not the agent's raises (`attentionNotifications.ts`); the agent's are its own read's `notices`. */
export function needsYouNotices(items: readonly NeedsYouItem[]): AttentionNotice[] {
  const out: AttentionNotice[] = []
  for (const item of items) {
    if (item.kind === 'run-grant') {
      out.push({ id: item.key, sessionId: null, reason: 'grant', message: `${runName(item.request)} asks for a grant`, href: NEEDS_YOU_PATH })
    } else if (item.kind === 'trigger-grant') {
      out.push({ id: item.key, sessionId: null, reason: 'grant', message: `Trigger of ${item.trigger.name} asks for a standing grant`, href: NEEDS_YOU_PATH })
    } else if (item.kind === 'pairing') {
      out.push({ id: item.key, sessionId: null, reason: 'pairing', message: `${item.pairing.label} asks to use a ScadBuddy tab`, href: NEEDS_YOU_PATH })
    }
  }
  return out
}

export const runName = (request: FlowRunGrantRequest): string => request.display_name || request.name

/** How many grant requests wait past the newest the API lists of each kind (`GRANT_REQUESTS_MAX`). */
export const notListed = (grants: FlowGrantRequests | null): number =>
  grants ? Math.max(0, grants.runs_total - grants.runs.length) + Math.max(0, grants.triggers_total - grants.triggers.length) : 0

/** The shell's list, and a way to read it again once one is answered; null outside the shell. */
export interface NeedsYou {
  /** Null until the first read answered (unknown is not "nothing waits"). */
  items: readonly NeedsYouItem[] | null
  /** How many more grant requests wait than `items` lists (`notListed`). */
  more: number
  refresh: () => void
  /** Opens the assistant panel, on the session when one is named; null while the assistant is not there. */
  openSession: ((sessionId: string | null) => void) | null
}

export const NeedsYouContext = createContext<NeedsYou | null>(null)

/** The title of an item's toast, as its OS notification's (`attentionNotifications.ts` `noticeTitle`). */
function toastTitle(item: NeedsYouItem): string {
  if (item.kind === 'run-grant' || item.kind === 'trigger-grant') return noticeTitle('grant')
  if (item.kind === 'pairing') return noticeTitle('pairing')
  const { entry } = item
  return noticeTitle(entry.kind === 'approval' ? 'approval' : (entry.reason ?? 'question'))
}

function toastDetail(item: NeedsYouItem): string {
  switch (item.kind) {
    case 'run-grant':
      return `Flow run ${runName(item.request)}`
    case 'trigger-grant':
      return `A trigger of ${item.trigger.name}`
    case 'pairing':
      return item.pairing.label
    case 'agent':
      return item.entry.run ? `Flow run ${item.entry.run.name}` : item.entry.tool
  }
}

/** How long a "Needs you" toast stays; the bell keeps the request after it goes. */
export const NEEDS_YOU_TOAST_MS = 30_000

/** Where an item is read from; each answers on its own, so the list is built from those that have. */
export type NeedsYouSource = 'agent' | 'grants' | 'pairings'

export const sourceOf = (item: NeedsYouItem): NeedsYouSource =>
  item.kind === 'agent' ? 'agent' : item.kind === 'pairing' ? 'pairings' : 'grants'

/**
 * #6399 — a toast for each request that starts to wait while this page is open, of every kind
 * but an MCP client's prepared action (`pendingApprovals.ts` `useApprovalToasts` toasts those, with
 * Deny): what was waiting at a source's first answer is the bell's to show, not announced again,
 * so an item is toasted only when its source (`known`) had answered before. "Review" opens
 * Activity's "Needs you"; the toast goes once its request no longer waits.
 */
export function useNeedsYouToasts(items: readonly NeedsYouItem[] | null, known: ReadonlySet<NeedsYouSource>): void {
  const seen = useRef(new Set<string>())
  const knew = useRef<ReadonlySet<NeedsYouSource>>(new Set())
  const shown = useRef(new Map<string, number>())
  useEffect(() => {
    if (items === null) return
    const keys = new Set(items.map((item) => item.key))
    for (const [key, id] of shown.current) {
      if (keys.has(key)) continue
      dismissToast(id)
      shown.current.delete(key)
    }
    const before = seen.current
    const answered = knew.current
    seen.current = keys
    knew.current = known
    for (const item of items) {
      if (before.has(item.key) || !answered.has(sourceOf(item))) continue
      if (item.kind === 'agent' && item.entry.kind === 'approval' && !item.entry.sessionId && !item.entry.run) continue
      const detail = toastDetail(item)
      shown.current.set(
        item.key,
        toast({
          title: toastTitle(item),
          ...(detail ? { detail } : {}),
          actions: [{ label: 'Review', href: NEEDS_YOU_PATH }],
          ms: NEEDS_YOU_TOAST_MS,
        }),
      )
    }
  }, [items, known])
  useEffect(() => {
    const live = shown.current
    return () => {
      for (const id of live.values()) dismissToast(id)
      live.clear()
    }
  }, [])
}

/** How long a burst of `workflow-runs` signals is merged before the grants are read again. */
const SIGNAL_DEBOUNCE_MS = 500

/**
 * The flow grants waiting for a decision: read at once, every `ATTENTION_POLL_MS` (the badge's
 * own poll), when the tab comes back into view, and shortly after a run moves (`workflow-runs`,
 * which a run that asks for a grant does as it starts). A failed read keeps the last answer.
 */
export function useGrantRequests(): { grants: FlowGrantRequests | null; refresh: () => void } {
  const [grants, setGrants] = useState<FlowGrantRequests | null>(null)
  const inflight = useRef(false)
  const again = useRef(false)
  const live = useRef(true)

  const refresh = useCallback(function read() {
    if (inflight.current) {
      again.current = true
      return
    }
    inflight.current = true
    again.current = false
    api.flowGrantRequests().then(
      (next) => {
        if (live.current) setGrants(next)
      },
      () => {},
    ).finally(() => {
      inflight.current = false
      if (again.current && live.current) read()
    })
  }, [])

  useEffect(() => {
    live.current = true
    refresh()
    const timer = setInterval(refresh, ATTENTION_POLL_MS)
    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', refresh)
    let debounce: ReturnType<typeof setTimeout> | undefined
    const stop = getRealtime().subscribe('workflow-runs', () => {
      clearTimeout(debounce)
      debounce = setTimeout(refresh, SIGNAL_DEBOUNCE_MS)
    })
    return () => {
      live.current = false
      stop()
      clearTimeout(debounce)
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', refresh)
    }
  }, [refresh])

  return { grants, refresh }
}
