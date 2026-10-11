import { ApiError } from '../api/client'
import type { FlowPending, FlowRunStatus } from '../api/types'

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

/** The backend's 409 for an answer or decision the run has moved past (`flows/operations.py`). */
export function isStaleEntry(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && (error.problem.type ?? '').endsWith('/stale-entry')
}

export const STALE_ENTRY_MESSAGE = 'This request is out of date. The run has moved on.'

/** The topic every run's change is published on, and one run's (backend `api/realtime.py`). */
export const FLOW_RUNS_TOPIC = 'workflow-runs'
export const flowRunTopic = (id: string) => `workflow-run:${id}`

/** How often the pages read while the realtime socket is unavailable. */
export const FLOW_POLL_MS = 5_000

export const flowRunPath = (id: string) => `/workflows/runs/${encodeURIComponent(id)}`
