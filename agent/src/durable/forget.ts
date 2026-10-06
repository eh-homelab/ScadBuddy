import { type Client, isGrpcServiceError, WorkflowNotFoundError } from '@temporalio/client'
import type { Sql } from 'postgres'
import type { AuditActor, AuditSink } from '../audit/log.js'
import type { PayloadKeys } from '../temporal/payloadKeys.js'

// forgetSubject (spec 2026-10-01 §6.5, plan task 13, #1056): the one operation every
// deletion of a durable session goes through. In the spec's order:
//   1. delete the subject's payload key, after which every copy of its payloads
//      (history, Visibility, Archival) is undecryptable;
//   2. terminate its workflow if it is open, then DeleteWorkflowExecution;
//   3. delete our rows, in one transaction.
// Only `session-<uuid>` is accepted; `flow-*` belongs to a later phase.
//
// A failure of step 2 throws ForgetIncompleteError and step 3 does NOT run: the
// session's rows stay, so the session is still found and the forget can be repeated.
// The key is already gone then (the payloads are unreadable, which is what matters
// first), and the repeat reports `keyDeleted: false` and finishes steps 2 and 3.

export type ForgetResult = {
  keyDeleted: boolean
  /** `absent`: no Temporal client is configured, or the workflow does not exist. */
  workflow: 'terminated' | 'closed' | 'absent'
  rows: number
}

export type ForgetDeps = {
  sql: Sql
  keys: PayloadKeys
  client?: Client
  /** Bounds every Temporal call of the workflow step, so an unreachable server fails instead of retrying forever. */
  rpcDeadlineMs?: number
}

export class ForgetIncompleteError extends Error {
  readonly step: 'workflow' | 'rows'
  readonly keyDeleted: boolean
  constructor(step: 'workflow' | 'rows', keyDeleted: boolean, cause: unknown) {
    super(`forget is incomplete: the ${step} step failed: ${cause instanceof Error ? cause.message : String(cause)}`, { cause })
    this.name = 'ForgetIncompleteError'
    this.step = step
    this.keyDeleted = keyDeleted
  }
}

const SESSION_SUBJECT = /^session-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/

/** How long deletion may take to show: it is asynchronous, and a dev server measured ~10 s after the close (polled with backoff, an upper bound). */
export const DELETE_WAIT_MS = 60_000
/** An absolute deadline for the whole step, longer than the wait. */
const RPC_DEADLINE_MS = DELETE_WAIT_MS + 30_000
const BACKOFF_FIRST_MS = 100
const BACKOFF_MAX_MS = 1_000

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

const isNotFound = (err: unknown): boolean =>
  err instanceof WorkflowNotFoundError || (isGrpcServiceError(err) && err.code === 5)

async function removeWorkflow(client: Client, workflowId: string): Promise<'terminated' | 'closed' | 'absent'> {
  const handle = client.workflow.getHandle(workflowId)
  let outcome: 'terminated' | 'closed'
  let runId: string
  try {
    const described = await handle.describe()
    runId = described.runId
    if (described.status.name === 'RUNNING') {
      await handle.terminate('forgetSubject')
      outcome = 'terminated'
    } else {
      outcome = 'closed'
    }
  } catch (err) {
    if (isNotFound(err)) return 'absent'
    throw err
  }
  const namespace = client.options.namespace
  // A monotonic clock: the host's wall clock may jump.
  const deadline = performance.now() + DELETE_WAIT_MS
  let delay = BACKOFF_FIRST_MS
  let deleteRequested = false
  let last: unknown
  for (;;) {
    try {
      if (!deleteRequested) {
        await client.workflowService.deleteWorkflowExecution({ namespace, workflowExecution: { workflowId, runId } })
        deleteRequested = true
      }
      await handle.describe()
    } catch (err) {
      if (isNotFound(err)) return outcome
      // A terminate that has not closed the run yet makes the delete wait its turn.
      if (!(isGrpcServiceError(err) && err.code === 9)) throw err
      last = err
    }
    if (performance.now() >= deadline) {
      throw new Error(
        `workflow ${workflowId} was still there ${DELETE_WAIT_MS / 1000} s after its deletion was requested${last instanceof Error ? `: ${last.message}` : ''}`,
      )
    }
    await sleep(delay)
    delay = Math.min(delay * 2, BACKOFF_MAX_MS)
  }
}

export async function forgetSubject(subject: string, deps: ForgetDeps): Promise<ForgetResult> {
  const match = SESSION_SUBJECT.exec(subject)
  if (!match) throw new Error(`${subject} is not a session-<uuid> subject`)
  const sessionId = match[1]!

  const [existing] = await deps.sql`SELECT 1 AS present FROM ai_payload_keys WHERE subject = ${subject}`
  await deps.keys.forget(subject)
  const keyDeleted = existing !== undefined

  let workflow: ForgetResult['workflow'] = 'absent'
  if (deps.client) {
    try {
      const client = deps.client
      workflow = await client.connection.withAbortSignal(AbortSignal.timeout(deps.rpcDeadlineMs ?? RPC_DEADLINE_MS), () =>
        removeWorkflow(client, subject),
      )
    } catch (err) {
      throw new ForgetIncompleteError('workflow', keyDeleted, err)
    }
  }

  try {
    const rows = await deps.sql.begin(async (tx) => {
      let count = 0
      // ai_session_entries is keyed by the Claude session id, not ours: one per segment attempt.
      const claudeIds = await tx<{ claude_session_id: string }[]>`
        SELECT DISTINCT claude_session_id FROM ai_durable_segments WHERE session_id = ${sessionId}`
      const ids = claudeIds.map((row) => row.claude_session_id)
      if (ids.length > 0) count += (await tx`DELETE FROM ai_session_entries WHERE session_id = ANY(${ids})`).count
      count += (await tx`DELETE FROM ai_durable_segments WHERE session_id = ${sessionId}`).count
      count += (await tx`DELETE FROM ai_durable_streams WHERE session_id = ${sessionId}`).count
      count += (await tx`DELETE FROM ai_durable_snapshots WHERE session_id = ${sessionId}`).count
      count += (await tx`DELETE FROM ai_session_events WHERE session_id = ${sessionId}`).count
      count += (await tx`DELETE FROM ai_sessions WHERE id = ${sessionId}`).count
      return count
    })
    return { keyDeleted, workflow, rows }
  } catch (err) {
    throw new ForgetIncompleteError('rows', keyDeleted, err)
  }
}

/** The CLI's whole behaviour but its wiring: what it prints, what it audits, how it exits. */
export async function forgetCli(
  subject: string,
  deps: ForgetDeps & { audit: AuditSink; actor: AuditActor },
): Promise<{ exitCode: 0 | 1; output: Record<string, unknown> }> {
  const sessionId = SESSION_SUBJECT.exec(subject)?.[1]
  const base = { kind: 'operator', action: 'forget_subject', surface: 'system', actor: deps.actor, sessionId } as const
  try {
    const result = await forgetSubject(subject, deps)
    const note = deps.client ? {} : { note: 'no Temporal address is configured: the workflow step was skipped' }
    await deps.audit.record({
      ...base,
      outcome: 'ok',
      inputSummary: subject,
      detail: JSON.stringify({ complete: true, ...result }),
    })
    return { exitCode: 0, output: { subject, ...result, ...note } }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    const incomplete = err instanceof ForgetIncompleteError
    // A refused subject deleted nothing, so there is nothing to audit.
    if (incomplete) {
      await deps.audit.record({
        ...base,
        outcome: 'error',
        inputSummary: subject,
        detail: JSON.stringify({ complete: false, failed_step: err.step, keyDeleted: err.keyDeleted, error: message }),
      })
    }
    return { exitCode: 1, output: { subject, error: message, ...(incomplete ? { complete: false, failed_step: err.step } : {}) } }
  }
}
