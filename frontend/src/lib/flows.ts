import { ApiError } from '../api/client'
import { RespondError } from '../agent/respond'
import type { FlowPending, FlowRunStatus, FlowSummary } from '../api/types'

/** #1057 — how a flow run's status reads on the Workflows pages. */
export const FLOW_STATUS_LABEL: Record<FlowRunStatus, string> = {
  starting: 'Starting…',
  running: 'Running',
  waiting: 'Waiting',
  succeeded: 'Succeeded',
  failed: 'Failed',
  terminated: 'Terminated',
}

/** What a parked call waits on, as one line. */
export function waitingText(entry: Pick<FlowPending, 'kind' | 'fn' | 'prompt'>): string {
  return entry.kind === 'answer'
    ? `Waiting for your answer: ${entry.prompt ?? ''}`
    : `Waiting for approval: ${entry.fn}`
}

/** One difference between a call's arguments and a parent's proposal (#2482). */
export type ProposalChange = { path: string; was: string; now: string }

const shown = (value: unknown): string => (typeof value === 'string' ? value : JSON.stringify(value))
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

/**
 * #2482 — what a parent's proposal changes in the call, field by field, in order: a
 * value changed, or a field or list item left out (`(removed)`) or added (`(added)`).
 */
export function proposalDiff(original: unknown, proposed: unknown, path = ''): ProposalChange[] {
  if (Array.isArray(original) && Array.isArray(proposed)) {
    const out: ProposalChange[] = []
    for (let i = 0; i < Math.max(original.length, proposed.length); i++) {
      const at = `${path}[${i}]`
      if (i >= proposed.length) out.push({ path: at, was: shown(original[i]), now: '(removed)' })
      else if (i >= original.length) out.push({ path: at, was: '(added)', now: shown(proposed[i]) })
      else out.push(...proposalDiff(original[i], proposed[i], at))
    }
    return out
  }
  if (isObject(original) && isObject(proposed)) {
    const keys = [...Object.keys(original), ...Object.keys(proposed).filter((k) => !(k in original))]
    return keys.flatMap((k) => {
      const at = path ? `${path}.${k}` : k
      if (!(k in proposed)) return [{ path: at, was: shown(original[k]), now: '(removed)' }]
      if (!(k in original)) return [{ path: at, was: '(added)', now: shown(proposed[k]) }]
      return proposalDiff(original[k], proposed[k], at)
    })
  }
  return JSON.stringify(original) === JSON.stringify(proposed) ? [] : [{ path, was: shown(original), now: shown(proposed) }]
}

/**
 * #2482 — when a hop ends, as a person reads it: the time alone today, with the date
 * otherwise, so a hop that runs past midnight never reads as already over.
 */
export function hopUntilText(iso: string, now: Date = new Date()): string {
  const at = new Date(iso)
  return at.toDateString() === now.toDateString() ? at.toLocaleTimeString() : at.toLocaleString()
}

/** #2482 — who did what to a call on its way up, as the run page lists it. */
export function hopActor(actor: string): string {
  if (actor === 'flow') return 'the flow'
  if (actor === 'system') return 'ScadBuddy'
  if (actor === 'browser') return 'you'
  if (actor.startsWith('session:')) return 'the starting session'
  return actor
}

/** The backend's 409 for an answer or decision the run has moved past (`flows/operations.py`). */
export function isStaleEntry(error: unknown): boolean {
  // A child session's call is answered through the agent, whose refusal is a RespondError.
  if (error instanceof RespondError) return error.settled
  return error instanceof ApiError && error.status === 409 && (error.problem.type ?? '').endsWith('/stale-entry')
}

export const STALE_ENTRY_MESSAGE = 'This request is out of date. The run has moved on.'

/** The topic every run's change is published on, and one run's (backend `api/realtime.py`). */
export const FLOW_RUNS_TOPIC = 'workflow-runs'
export const flowRunTopic = (id: string) => `workflow-run:${id}`

/** How often the pages read while the realtime socket is unavailable. */
export const FLOW_POLL_MS = 5_000

export const flowRunPath = (id: string) => `/workflows/runs/${encodeURIComponent(id)}`

/** #2350 — the Flows tab, and one flow version's page. */
export const FLOWS_PATH = '/workflows/flows'
/**
 * #3186 — a flow's Markdown description as one line of plain text, for a list: the
 * markup (headings, emphasis, code marks, links' targets, list and quote markers)
 * dropped, the words kept, every run of blanks one space.
 */
export function markdownText(markdown: string): string {
  return markdown
    .replace(/^```.*$/gm, '')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/gm, '')
    .replace(/`([^`]*)`/g, '$1')
    // Emphasis hugs its words: `2 * 3` and snake_case are not emphasis.
    .replace(/(\*\*|\*|~~)(?=\S)(.+?)(?<=\S)\1/g, '$2')
    .replace(/(^|[^\w])(__?)(?=\S)(.+?)(?<=\S)\2(?!\w)/g, '$1$3')
    .replace(/\s+/g, ' ')
    .trim()
}

/** #3186 — what a flow is called: its display name, else its name (the slug, its id). */
export const flowTitle = (flow: { name: string; display_name?: string | null }) => flow.display_name || flow.name

export const flowPath = (id: string) => `${FLOWS_PATH}/${encodeURIComponent(id)}`

/** The statuses a run has ended in: forgetting one stops nothing. */
export const FLOW_TERMINAL: ReadonlySet<FlowRunStatus> = new Set(['succeeded', 'failed', 'terminated'])

/** Who registered a flow or started a run (`created_by`, `started_by`), as a phrase. */
export function actorText(actor: Record<string, unknown>): string {
  if (actor['kind'] === 'browser') return 'you, in the browser'
  // The build's own flows (#4252: the built-in `order-fulfilment`).
  if (actor['kind'] === 'builtin') return 'ScadBuddy (built in)'
  if (actor['kind'] !== 'agent') return 'someone else'
  const session = actor['session']
  return typeof session === 'string' && session ? `an assistant (session ${session})` : 'an assistant'
}

/** #2485 — a flow archived or deleted, and who did it, as a phrase; null for neither. */
export function flowStateText(
  flow: Pick<FlowSummary, 'archived_at' | 'archived_by' | 'deleted_at' | 'deleted_by'>,
): string | null {
  if (flow.deleted_at) return `Deleted by ${actorText(flow.deleted_by ?? {})}`
  if (flow.archived_at) return `Archived by ${actorText(flow.archived_by ?? {})}`
  return null
}
