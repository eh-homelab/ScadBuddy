import type { Client } from '@temporalio/client'
import { WorkflowNotFoundError } from '@temporalio/common'
import type { Sql } from 'postgres'
import { backgroundWorkflowId } from '../background/service.js'
import { terminateOpenToolCalls } from '../tools/activityProjection.js'
import { type PayloadKeys, payloadKeyLock, subjectOf } from '../temporal/payloadCodec.js'
import { copyForkRanges, ForksNeedKeys, hasForks } from './store.js'
import { notifyWarmEvict } from './warmEvict.js'

// forgetSubject (spec 2026-10-01 §6.5, plan 5c Ruling 12): the one operation every
// deletion of a durable session or a flow run goes through. In this order:
//   0. a session's background tasks (src/background/) end: their rows, then their
//      workflows `bgtask-<session>-<id>`, terminated, so none encodes again under the key;
//      and its tool calls still open (`session-<id>-call-<tool_use_id>`, plan 2026-10-13
//      Step 9.2: every `tool.call` in its events with no `tool.result`) are terminated too;
//   1. a session's forks that read its transcript through it (store.ts, plan 2026-10-13
//      Step 6.5) take the range each references, sealed under their own keys, in the
//      transaction that then goes on to
//      delete the subject's `ai_payload_keys` row and record it in
//      `ai_forgotten_subjects`, so no key is ever made for it again: from then on every
//      copy of its payloads (history, Visibility, Archival) is undecryptable, whatever
//      fails next. A flow run's images (`ai_flow_blobs`, #2477) are deleted with it.
//      A process that read the key keeps it in memory 30 s at most
//      (payloadCodec.ts, codec.py);
//      Its large payloads, files on the data share (#2243), are ciphertext under that
//      key, so they are unreadable from here on too; step 3 removes them;
//   2. terminate the workflow if it is open, then DeleteWorkflowExecution;
//   3. remove its stored payloads' directory (when this process has the share), and
//      delete our rows, as a session's deletion always did: `ai_sessions`, whose
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
  /**
   * Drops the subject's data key from this process's cache too, and opens and seals the
   * transcript a session's forks take (step 1); a session with forks is not forgotten without it.
   */
  keys?: (PayloadKeys & { forget(subject: string): void }) | undefined
  /** Where its large payloads are kept (FilePayloadStore); without one, they stay, unreadable. */
  payloads?: { forget(subject: string): Promise<void> } | undefined
  /** Its saved http bodies on this pod (turnServers.ts `removeTurnDir`); other pods' go to their sweep. */
  turnDirs?: ((subject: string) => Promise<void>) | undefined
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

async function endBackgroundTasks(deps: ForgetDeps, sessionId: string): Promise<void> {
  const ended = await deps.sql<{ id: string }[]>`
    UPDATE ai_background_tasks SET state = 'cancelled', detail = 'the chat was forgotten', next_at = NULL, updated_at = now()
     WHERE session_id = ${sessionId} AND state = 'active' RETURNING id`
  for (const { id } of ended) {
    try {
      await deps.client?.workflow.getHandle(backgroundWorkflowId(sessionId, id)).terminate('forgotten (forgetSubject)')
    } catch (err) {
      if (!notFound(err)) throw err
    }
  }
}

export async function forgetSubject(deps: ForgetDeps, subject: string): Promise<Forgotten> {
  if (subjectOf(subject) !== subject) throw new Error(`${subject} is not a session-<uuid> or flow-<uuid> subject`)
  const sessionId = subject.startsWith('session-') ? subject.slice('session-'.length) : undefined
  // Said before anything ends; the check that holds is copyForkRanges's, under the lock.
  if (sessionId !== undefined && !deps.keys && (await hasForks(deps.sql, sessionId))) throw new ForksNeedKeys(sessionId)
  deps.keys?.forget(subject)
  if (sessionId !== undefined) await endBackgroundTasks(deps, sessionId)
  if (sessionId !== undefined && deps.client) await terminateOpenToolCalls({ sql: deps.sql, client: deps.client }, sessionId, 'forgotten (forgetSubject)')
  // The tombstone with the deletion: no encoder still running for the subject (a
  // workflow task or an activity before the termination below) can make it a new key.
  const keys = await deps.sql.begin(async (tx) => {
    // The lock key creation takes (payloadCodec.ts), so no encoder's tombstone check
    // reads from before this commits.
    await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
    // While the key still opens its transcript; a fork made later sees the tombstone (manager.ts insert).
    if (sessionId !== undefined) await copyForkRanges(tx, deps.keys, sessionId)
    await tx`INSERT INTO ai_forgotten_subjects (subject) VALUES (${subject}) ON CONFLICT (subject) DO NOTHING`
    // A flow run's images go with its key (#2477, temporal/flowBlobs.ts).
    if (subject.startsWith('flow-')) await tx`DELETE FROM ai_flow_blobs WHERE subject = ${subject}`
    return tx`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
  })
  // Again: copyForkRanges read the key to open the transcript, which cached it in this process.
  deps.keys?.forget(subject)
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
  await deps.payloads?.forget(subject)
  await deps.turnDirs?.(subject)
  let rows = 0
  if (subject.startsWith('session-')) {
    const id = subject.slice('session-'.length)
    const entries = await deps.sql`DELETE FROM ai_session_entries WHERE session_id = ${id}`
    const sessions = await deps.sql`DELETE FROM ai_sessions WHERE id = ${id}`
    rows = entries.count + sessions.count
    // A classic session's warm process, on whichever replica holds it (spec 2026-10-13 §5.3, warmEvict.ts).
    await notifyWarmEvict(deps.sql, id)
  }
  return { key: keys.count > 0, workflow, rows }
}

/** How old a session's payload key is before sweepOrphanedSessionKeys looks at it: well past any insert it could race. */
export const ORPHAN_KEY_MIN_AGE_S = 24 * 3600

/**
 * Deletes the payload keys of sessions whose `ai_sessions` row is gone (#3317), and returns how
 * many. forgetSubject deletes its own; what this catches is a durable fork whose workflow could
 * not be started (SessionManager.fork deletes the child's row), whose start had already made the
 * child's key. Older than `minAgeS` only, and checked again under the subject's lock, which a key's
 * creation takes too. Not a forget: no tombstone, as no session can take that id again.
 */
export async function sweepOrphanedSessionKeys(sql: Sql, minAgeS = ORPHAN_KEY_MIN_AGE_S): Promise<number> {
  const subjects = await sql<{ subject: string }[]>`
    SELECT k.subject FROM ai_payload_keys k
    WHERE k.subject LIKE 'session-%' AND k.created_at < now() - make_interval(secs => ${minAgeS})
      AND NOT EXISTS (SELECT 1 FROM ai_sessions s WHERE s.id::text = substr(k.subject, 9))
    LIMIT 100`
  let deleted = 0
  for (const { subject } of subjects) {
    const id = subject.slice('session-'.length)
    deleted += await sql.begin(async (tx) => {
      await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
      const gone = await tx`
        DELETE FROM ai_payload_keys WHERE subject = ${subject}
          AND NOT EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = ${id})
          AND NOT EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id::text = ${id})`
      return gone.count
    })
  }
  return deleted
}
