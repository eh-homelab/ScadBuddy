import type { Client } from '@temporalio/client'
import type { Sql } from 'postgres'
import { CrudReadError, crudRead } from '../api/crudRead.js'
import type { CrudAreas } from '../api/crudAreas.js'
import { crudOf, type CrudTarget } from '../api/crudStores.js'
import { backgroundWorkflowId } from '../background/service.js'
import { BackgroundTaskStore, CrudBackgroundTasks } from '../background/store.js'
import { unscheduleTask } from '../background/temporal.js'
import { terminateOpenToolCalls, workflowNotFound as notFound } from '../tools/activityProjection.js'
import { dropPayloadKey, type PayloadKeys, payloadKeyLock, subjectOf, sweepBridgeKeys } from '../temporal/payloadCodec.js'
import { dropUnnamedBytes } from '../workspace/store.js'
import { copyForkRanges, ForksNeedKeys, hasForks } from './store.js'
import { sessionPath } from './rows.js'
import { CrudSessionReads, PgSessionReads, type SessionReads } from './sessionReads.js'
import { notifyWarmEvict } from './warmEvict.js'

// forgetSubject (spec 2026-10-01 §6.5, plan 5c Ruling 12): the one operation every
// deletion of a durable session or a flow run goes through. In this order:
//   0. a session's background tasks (src/background/) end: their rows, then their
//      workflows `bgtask-<session>-<id>`, terminated, so none encodes again under the key;
//      and its tool calls still open (`session-<id>-call-<tool_use_id>`, plan 2026-10-13
//      Step 9.2: every `tool.call` in its events with no `tool.result`) are terminated too;
//   1. a session's forks that read its transcript through it (store.ts, plan 2026-10-13
//      Step 6.5) take the range each references, sealed under their own keys, with the
//      events and images they read through it (#4212), in the
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
//   3. remove its stored payloads' directory (when this process has the share) and a session's
//      working directory on this pod, and
//      delete our rows, as a session's deletion always did: `ai_sessions`, whose
//      cascade takes its events, `ai_pending_input`, `ai_input_responses`, blobs,
//      resources, approvals and questions, and its workspace files, and `ai_session_entries`,
//      which is not keyed to it; then its files' bytes kept in Postgres
//      (`ai_session_file_bytes`) that no other workspace, a fork's among them, names. The
//      audit log keeps its rows, as it does for every session.
// Step 1's transaction is the API's once it lists area `forget` (#5299 step 6, backend
// api/crud_agent_forget.py): `CrudSubjectKeys.forget` asks `POST …/crud/agent/forgotten-subjects/
// {subject}`, which takes the lock, writes the tombstone and deletes a flow run's images and the
// key in one transaction, a session's forks first taking their ranges of its transcript, events
// and images there (backend transcripts.py `copy_fork_ranges`, #5299 step 9, part c). Only an API
// with no KEK, whose forks take transcript entries, refuses it (`forks`): its forget then runs the
// transaction below, only when the agent has no route for step 3 either (no `rows`): with one,
// the forget refuses it (ForksNeedKeys) and deletes nothing more. Under the same lock, so either
// way a fork made later sees the tombstone.
// Step 3's rows, their unnamed bytes and the warm eviction are the API's once it lists area
// `forget-rows` (#6501, backend api/crud_agent_sweeps.py, `CrudForgetRows`); step 0 reads the
// tasks and the open calls through `tasks` and `reads`, which are the API's once it lists their
// areas.
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
  /**
   * A session's working directory (workspace/directory.ts `removeSessionDir`), its workspace's
   * plaintext copy, on this pod; another worker's copy goes with its sweep once the row is gone.
   */
  sessionDirs?: ((sessionId: string) => Promise<void>) | undefined
  /** Step 1 through the API (`CrudSubjectKeys`, above), when it lists area `forget`; the SQL below otherwise. */
  crud?: Pick<CrudSubjectKeys, 'forget'> | undefined
  /** A session's background tasks (step 0); `BackgroundTaskStore` over `sql` without it. */
  tasks?: Pick<BackgroundTaskStore, 'list' | 'finish'> | undefined
  /** A session's open tool calls (step 0); `PgSessionReads` over `sql` without it. */
  reads?: Pick<SessionReads, 'openCalls'> | undefined
  /** Step 3 through the API (`CrudForgetRows`), when it lists area `forget-rows`; the SQL below otherwise. */
  rows?: Pick<CrudForgetRows, 'forget'> | undefined
}

export type Forgotten = {
  key: boolean
  workflow: 'deleted' | 'not_found' | 'not_reached'
  rows: number
}

async function endBackgroundTasks(deps: ForgetDeps, sessionId: string): Promise<void> {
  const tasks = deps.tasks ?? new BackgroundTaskStore(deps.sql)
  for (const { id } of await tasks.list(sessionId)) {
    await tasks.finish(id, 'cancelled', 'the chat was forgotten')
    // A recurring cron job's Schedule and its running fire (plan 2026-10-13 Step 9.8), or any
    // task's workflow; also when it ended meanwhile, so nothing of it runs on.
    if (deps.client) await unscheduleTask(deps.client, { id, sessionId }, 'forgotten (forgetSubject)')
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
  // Without the API's route, said before anything ends; the check that holds is copyForkRanges's,
  // under the lock. With the route the API decides under its lock and copies the ranges with its
  // own KEK. Only an API with no KEK refuses (`forks-take-ranges`), and then the fallback below
  // throws ForksNeedKeys after the tasks have ended: the subject stays, and the forget can run again.
  if (sessionId !== undefined && !deps.crud && !deps.keys && (await hasForks(deps.sql, sessionId))) throw new ForksNeedKeys(sessionId)
  deps.keys?.forget(subject)
  if (sessionId !== undefined) await endBackgroundTasks(deps, sessionId)
  if (sessionId !== undefined && deps.client) await terminateOpenToolCalls({ reads: deps.reads ?? new PgSessionReads(deps.sql), client: deps.client }, sessionId, 'forgotten (forgetSubject)')
  // The tombstone with the deletion: no encoder still running for the subject (a
  // workflow task or an activity before the termination below) can make it a new key.
  const answered = deps.crud ? await deps.crud.forget(subject) : 'forks'
  // An API with no KEK whose forks take transcript entries, while step 3 is the API's: no SQL here.
  if (answered === 'forks' && deps.crud && deps.rows && sessionId !== undefined) throw new ForksNeedKeys(sessionId, { terminated: deps.client !== undefined })
  const key =
    answered !== 'forks'
      ? answered
      : await deps.sql.begin(async (tx) => {
          // The lock key creation takes (payloadCodec.ts), so no encoder's tombstone check
          // reads from before this commits.
          await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
          // While the key still opens its transcript; a fork made later sees the tombstone (manager.ts insert).
          if (sessionId !== undefined) await copyForkRanges(tx, deps.keys, sessionId)
          await tx`INSERT INTO ai_forgotten_subjects (subject) VALUES (${subject}) ON CONFLICT (subject) DO NOTHING`
          // A flow run's images go with its key (#2477, temporal/flowBlobs.ts).
          if (subject.startsWith('flow-')) await tx`DELETE FROM ai_flow_blobs WHERE subject = ${subject}`
          const deleted = await tx`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
          return deleted.count > 0
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
  if (sessionId !== undefined) await deps.sessionDirs?.(sessionId)
  let rows = 0
  if (sessionId !== undefined && deps.rows) {
    rows = await deps.rows.forget(sessionId)
  } else if (subject.startsWith('session-')) {
    const id = subject.slice('session-'.length)
    const shared = await deps.sql<{ bytes_sha256: string }[]>`
      SELECT DISTINCT bytes_sha256 FROM ai_session_files WHERE session_id = ${id} AND bytes_sha256 IS NOT NULL`
    const entries = await deps.sql`DELETE FROM ai_session_entries WHERE session_id = ${id}`
    const sessions = await deps.sql`DELETE FROM ai_sessions WHERE id = ${id}`
    rows = entries.count + sessions.count
    // A write naming the same bytes meanwhile fails this on the foreign key: the sweep deletes them later.
    await dropUnnamedBytes(
      deps.sql,
      shared.map((row) => row.bytes_sha256),
    ).catch((err: unknown) => console.error(`forgetSubject: could not delete unnamed workspace bytes; the sweep deletes them later: ${(err as Error).message}`))
    // A classic session's warm process, on whichever replica holds it (spec 2026-10-13 §5.3, warmEvict.ts).
    await notifyWarmEvict(deps.sql, id)
  }
  return { key, workflow, rows }
}

/**
 * forgetSubject's step 3 through the API's CRUD route (#6501, area `forget-rows`): a forgotten
 * session's transcript rows and its row, then its unnamed workspace bytes and the warm eviction,
 * as the SQL above. The API refuses a session with no tombstone (409 `not-forgotten`).
 */
export class CrudForgetRows {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  /** How many rows went: its transcript rows and its session row. */
  async forget(sessionId: string): Promise<number> {
    const path = sessionPath(sessionId)
    if (path === undefined) throw new Error(`session ${JSON.stringify(sessionId)} is not a session id`)
    const { rows } = await this.#call<{ rows: number }>(`the rows of session ${sessionId}`, `${path}/forget-rows`, { method: 'POST' })
    return rows
  }
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

/** The deletes of payload keys no forget makes: a browser call's once it has its answer, and the hourly sweep. */
export interface KeyDeletes {
  /** A browser call's key (`bridge-<uuid>`); no tombstone. */
  dropBridgeKey(subject: string): Promise<void>
  /** The keys of sessions whose row is gone and of browser calls long over; how many went. */
  sweep(): Promise<number>
}

/** The SQL the agent runs itself (`dropPayloadKey`, `sweepOrphanedSessionKeys`, `sweepBridgeKeys`) until the API lists area `forget`. */
export class PgKeyDeletes implements KeyDeletes {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  dropBridgeKey(subject: string): Promise<void> {
    return dropPayloadKey(this.#sql, undefined, subject)
  }

  async sweep(): Promise<number> {
    return (await sweepOrphanedSessionKeys(this.#sql)) + (await sweepBridgeKeys(this.#sql))
  }
}

/**
 * Forgetting a subject, and deleting the keys nothing forgets, through the API's CRUD routes
 * (#5299 step 6, area `forget`, backend api/crud_agent_forget.py): each one transaction there,
 * under the subject's lock, as the SQL here runs it.
 */
export class CrudSubjectKeys implements KeyDeletes {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  /**
   * forgetSubject's step 1: whether the subject had a key; `forks` for a session whose forks take
   * transcript entries when the API has no KEK to copy them, which forgetSubject's own
   * transaction then does.
   */
  async forget(subject: string): Promise<boolean | 'forks'> {
    try {
      const { key } = await crudRead<{ key: boolean }>(this.#fetch, this.#base, `the forget of ${subject}`, `/forgotten-subjects/${subject}`, {
        method: 'POST',
        body: {},
      })
      return key
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 409 && err.reason === 'forks-take-ranges') return 'forks'
      throw err
    }
  }

  async dropBridgeKey(subject: string): Promise<void> {
    await crudRead(this.#fetch, this.#base, `the key of ${subject}`, `/payload-keys/${subject}/drop`, { method: 'POST', body: {} })
  }

  async sweep(): Promise<number> {
    const { orphaned, bridge } = await crudRead<{ orphaned: number; bridge: number }>(this.#fetch, this.#base, 'the payload key sweep', '/payload-key-sweeps', {
      method: 'POST',
      body: {},
      // One transaction per orphaned key, at most 100 of them.
      timeoutMs: 60_000,
    })
    return orphaned + bridge
  }
}

/** `crud` once `useCrud()` holds (the API lists area `forget`), `pg` until then, chosen per call (#5299 D9). */
export function gatedKeyDeletes(pg: KeyDeletes, crud: KeyDeletes, useCrud: () => boolean): KeyDeletes {
  return {
    dropBridgeKey: (subject) => (useCrud() ? crud : pg).dropBridgeKey(subject),
    sweep: () => (useCrud() ? crud : pg).sweep(),
  }
}

/** The areas the operator's forget (`forget-subject.ts`) asks the API for (#6501). */
export const FORGET_CLI_AREAS = ['forget', 'background-tasks', 'session-reads', 'forget-rows'] as const

/**
 * The forget's routes as the API lists them, for `forget-subject.ts`: the key transaction once
 * `forget` is listed; the background tasks per call; a session's open calls once
 * `session-reads` is; and step 3's rows only when both `forget` and `forget-rows` are, so a
 * forget never deletes, through the API, the rows of a session it did not tombstone there.
 */
export function forgetRoutes(
  areas: Pick<CrudAreas, 'has'>,
  target: CrudTarget,
  sql: Sql,
): Pick<ForgetDeps, 'crud' | 'tasks' | 'reads' | 'rows'> {
  const crud = areas.has('forget') ? new CrudSubjectKeys(target.baseUrl, { fetch: target.fetch }) : undefined
  return {
    crud,
    tasks: new BackgroundTaskStore(sql, { crud: new CrudBackgroundTasks(target.baseUrl, { fetch: target.fetch }), use: () => areas.has('background-tasks') }),
    reads: areas.has('session-reads') ? new CrudSessionReads(target) : undefined,
    rows: crud && areas.has('forget-rows') ? new CrudForgetRows(target) : undefined,
  }
}
