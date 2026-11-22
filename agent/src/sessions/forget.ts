import type { Client } from '@temporalio/client'
import { CrudReadError, crudRead } from '../api/crudRead.js'
import { crudOf, type CrudTarget } from '../api/crudStores.js'
import { backgroundWorkflowId } from '../background/service.js'
import { BackgroundTaskStore, CrudBackgroundTasks } from '../background/store.js'
import { unscheduleTask } from '../background/temporal.js'
import { terminateOpenToolCalls, workflowNotFound as notFound } from '../tools/activityProjection.js'
import { type ForgetsKeys, subjectOf } from '../temporal/payloadCodec.js'
import { ForksNeedKeys } from './store.js'
import { sessionPath } from './rows.js'
import { CrudSessionReads, type SessionReads } from './sessionReads.js'

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
// Step 1's transaction is the API's (#5299 step 6, backend api/crud_agent_forget.py):
// `CrudSubjectKeys.forget` asks `POST …/crud/agent/forgotten-subjects/{subject}`, which takes the
// lock, writes the tombstone and deletes a flow run's images and the key in one transaction, a
// session's forks first taking their ranges of its transcript, events and images there (backend
// transcripts.py `copy_fork_ranges`, #5299 step 9, part c). Only an API with no KEK, whose forks
// take transcript entries, refuses it (`forks`): the forget then throws ForksNeedKeys and deletes
// nothing more.
// Step 3's rows, their unnamed bytes and the warm eviction are the API's too (#6501, area
// `forget-rows`, backend api/crud_agent_sweeps.py, `CrudForgetRows`); step 0 reads the tasks and
// the open calls through the API.
// Its callers: an operator (`node dist/forget-subject.js <subject>`) and any delete
// route added later. `SessionStore.delete` is never reached for a durable session,
// whose conversation is in its workflow, not in ai_session_entries.

export type ForgetDeps = {
  /** The Temporal client; without one, the workflow is left to the namespace's retention. */
  client?: Client | undefined
  namespace?: string
  /** Drops the subject's data key from this process's cache too. */
  keys?: ForgetsKeys | undefined
  /** Where its large payloads are kept (FilePayloadStore); without one, they stay, unreadable. */
  payloads?: { forget(subject: string): Promise<void> } | undefined
  /** Its saved http bodies on this pod (turnServers.ts `removeTurnDir`); other pods' go to their sweep. */
  turnDirs?: ((subject: string) => Promise<void>) | undefined
  /**
   * A session's working directory (workspace/directory.ts `removeSessionDir`), its workspace's
   * plaintext copy, on this pod; another worker's copy goes with its sweep once the row is gone.
   */
  sessionDirs?: ((sessionId: string) => Promise<void>) | undefined
  /** Step 1 (`CrudSubjectKeys`, above). */
  crud: Pick<CrudSubjectKeys, 'forget'>
  /** A session's background tasks (step 0). */
  tasks: Pick<BackgroundTaskStore, 'list' | 'finish'>
  /** A session's open tool calls (step 0). */
  reads: Pick<SessionReads, 'openCalls'>
  /** Step 3 (`CrudForgetRows`). */
  rows: Pick<CrudForgetRows, 'forget'>
}

export type Forgotten = {
  key: boolean
  workflow: 'deleted' | 'not_found' | 'not_reached'
  rows: number
}

async function endBackgroundTasks(deps: ForgetDeps, sessionId: string): Promise<void> {
  const tasks = deps.tasks
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
  deps.keys?.forget(subject)
  if (sessionId !== undefined) await endBackgroundTasks(deps, sessionId)
  if (sessionId !== undefined && deps.client) await terminateOpenToolCalls({ reads: deps.reads, client: deps.client }, sessionId, 'forgotten (forgetSubject)')
  // The tombstone with the deletion: no encoder still running for the subject (a
  // workflow task or an activity before the termination below) can make it a new key.
  const key = await deps.crud.forget(subject)
  // An API with no KEK whose forks take transcript entries: the subject stays, and the forget can run again.
  if (key === 'forks') {
    if (sessionId === undefined) throw new Error(`the API refused the forget of ${subject}: forks-take-ranges`)
    throw new ForksNeedKeys(sessionId, { terminated: deps.client !== undefined })
  }
  // Again: a call that read the key meanwhile cached it in this process.
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
  const rows = sessionId !== undefined ? await deps.rows.forget(sessionId) : 0
  return { key, workflow, rows }
}

/**
 * forgetSubject's step 3 through the API's CRUD route (#6501, area `forget-rows`): a forgotten
 * session's transcript rows and its row, then its unnamed workspace bytes and the warm eviction,
 * in one transaction. The API refuses a session with no tombstone (409 `not-forgotten`).
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

/** The deletes of payload keys no forget makes: a browser call's once it has its answer, and the hourly sweep. */
export interface KeyDeletes {
  /** A browser call's key (`bridge-<uuid>`); no tombstone. */
  dropBridgeKey(subject: string): Promise<void>
  /** The keys of sessions whose row is gone and of browser calls long over; how many went. */
  sweep(): Promise<number>
}

/**
 * Forgetting a subject, and deleting the keys nothing forgets, through the API's CRUD routes
 * (#5299 step 6, area `forget`, backend api/crud_agent_forget.py): each one transaction there,
 * under the subject's lock. The hourly sweep deletes the keys of sessions whose row is gone
 * (#3317) and of browser calls long over.
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
   * transcript entries when the API has no KEK to copy them.
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

/** The forget's routes on the API, for `forget-subject.ts`. */
export function forgetRoutes(target: CrudTarget): Pick<ForgetDeps, 'crud' | 'tasks' | 'reads' | 'rows'> {
  return {
    crud: new CrudSubjectKeys(target.baseUrl, { fetch: target.fetch }),
    tasks: new BackgroundTaskStore(new CrudBackgroundTasks(target.baseUrl, { fetch: target.fetch })),
    reads: new CrudSessionReads(target),
    rows: new CrudForgetRows(target),
  }
}
