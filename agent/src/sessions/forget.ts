import type { Client } from '@temporalio/client'
import { WorkflowNotFoundError } from '@temporalio/common'
import type { Sql } from 'postgres'
import { subjectOf } from '../temporal/payloadCodec.js'

// forgetSubject (spec 2026-10-01 §6.5, plan 5c Ruling 12): the one operation every
// deletion of a durable session or a flow run goes through. In this order:
//   1. delete the subject's `ai_payload_keys` row and record it in
//      `ai_forgotten_subjects`, so no key is ever made for it again: from then on every
//      copy of its payloads (history, Visibility, Archival) is undecryptable, whatever
//      fails next. A process that read the key keeps it in memory 30 s at most
//      (payloadCodec.ts, codec.py);
//   2. terminate the workflow if it is open, then DeleteWorkflowExecution;
//   3. delete our rows, as a session's deletion always did: `ai_sessions`, whose
//      cascade takes its events, `ai_pending_input`, `ai_input_responses`, blobs,
//      resources, approvals and questions, and `ai_session_entries`, which is not keyed
//      to it. The audit log keeps its rows, as it does for every session.
// Its callers: an operator (`node dist/forget-subject.js <subject>`) and any delete
// route added later. `SessionStore.delete` is never reached for a durable session,
// whose conversation is in its workflow, not in ai_session_entries.

export type ForgetDeps = {
  sql: Sql
  /** The Temporal client; without one, the workflow is left to the namespace's retention. */
  client?: Client | undefined
  namespace?: string
  /** Drops the subject's data key from this process's cache too. */
  keys?: { forget(subject: string): void } | undefined
}

export type Forgotten = {
  key: boolean
  workflow: 'deleted' | 'not_found' | 'not_reached'
  rows: number
}

/** A gRPC NOT_FOUND from the workflow service. */
function notFound(err: unknown): boolean {
  return err instanceof WorkflowNotFoundError || (err as { code?: unknown } | null)?.code === 5
}

export async function forgetSubject(deps: ForgetDeps, subject: string): Promise<Forgotten> {
  if (subjectOf(subject) === undefined) throw new Error(`${subject} is not a session-<uuid> or flow-<uuid> subject`)
  deps.keys?.forget(subject)
  // The tombstone with the deletion: no encoder still running for the subject (a
  // workflow task or an activity before the termination below) can make it a new key.
  const keys = await deps.sql.begin(async (tx) => {
    await tx`INSERT INTO ai_forgotten_subjects (subject) VALUES (${subject}) ON CONFLICT (subject) DO NOTHING`
    return tx`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
  })
  let workflow: Forgotten['workflow'] = 'not_reached'
  if (deps.client) {
    const client = deps.client
    try {
      await client.workflow.getHandle(subject).terminate('forgotten (forgetSubject)')
    } catch (err) {
      if (!notFound(err)) throw err
    }
    try {
      await client.workflowService.deleteWorkflowExecution({
        namespace: deps.namespace ?? client.options.namespace,
        workflowExecution: { workflowId: subject },
      })
      workflow = 'deleted'
    } catch (err) {
      if (!notFound(err)) throw err
      workflow = 'not_found'
    }
  }
  let rows = 0
  if (subject.startsWith('session-')) {
    const id = subject.slice('session-'.length)
    const entries = await deps.sql`DELETE FROM ai_session_entries WHERE session_id = ${id}`
    const sessions = await deps.sql`DELETE FROM ai_sessions WHERE id = ${id}`
    rows = entries.count + sessions.count
  }
  return { key: keys.count > 0, workflow, rows }
}
