import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import { crudRead, wellFormed } from '../api/crudRead.js'
import type { Owner } from '../sessions/protocol.js'

// A session's background tasks (ai_background_tasks, migration
// 20261013T0445Z_background_tasks.sql): what CronCreate, ScheduleWakeup, Monitor and
// Workflow left running past their turn. The store is the record the panel lists; each
// active row's workflow (temporal/backgroundTask.ts) is what fires it.
//
// Since #5299 step 8 the API owns these tables: once `GET …/crud/agent/areas` lists
// `background-tasks` (api/crudAreas.ts), every method goes through `CrudBackgroundTasks`, the
// backend's api/crud_agent_tasks.py routes, one transaction each; until then, its own SQL.

export type TaskKind = 'cron' | 'wakeup' | 'monitor' | 'workflow'
export type TaskState = 'active' | 'done' | 'cancelled' | 'failed'

export type BackgroundTask = {
  id: string
  sessionId: string
  kind: TaskKind
  description: string
  spec: Record<string, unknown>
  createdBy: Owner
  state: TaskState
  nextAt: Date | null
  fired: number
  lastFiredAt: Date | null
  expiresAt: Date | null
  detail: string | null
  createdAt: Date
  updatedAt: Date
}

/** At most this many tasks active in one session at once. */
export const MAX_ACTIVE = 10
/** A monitor that makes more events than this is stopped ("Monitors that produce too many events are automatically stopped"). */
export const MAX_EVENTS = 200
/** One event's text at most (its lines past this are cut). */
export const EVENT_MAX_CHARS = 8000
/** One recorded notice at most, wrapper included (the column's cap): its text is cut first (monitor.ts eventNotice). */
export const NOTICE_MAX_CHARS = 16384
/**
 * One delivery's events at most, by their recorded text: MAX_EVENTS of NOTICE_MAX_CHARS
 * would be ~3 MB in one message, past any context, and a classic session would then
 * fail every turn after it on the same transcript. The rest go in the next delivery.
 */
export const DELIVER_MAX_CHARS = 64_000

type Row = {
  id: string
  session_id: string
  kind: TaskKind
  description: string
  spec: Record<string, unknown>
  created_by: Owner
  state: TaskState
  next_at: Date | null
  fired: number
  last_fired_at: Date | null
  expires_at: Date | null
  detail: string | null
  created_at: Date
  updated_at: Date
}

const task = (r: Row): BackgroundTask => ({
  id: r.id,
  sessionId: r.session_id,
  kind: r.kind,
  description: r.description,
  spec: r.spec,
  createdBy: r.created_by,
  state: r.state,
  nextAt: r.next_at,
  fired: r.fired,
  lastFiredAt: r.last_fired_at,
  expiresAt: r.expires_at,
  detail: r.detail,
  createdAt: r.created_at,
  updatedAt: r.updated_at,
})

export type NewTask = {
  sessionId: string
  kind: TaskKind
  description: string
  spec: Record<string, unknown>
  createdBy: Owner
  nextAt?: Date | undefined
  expiresAt?: Date | undefined
}

/** The CRUD routes while the API serves them (`use`), else the store's own SQL. */
export type RemoteBackgroundTasks = { crud: CrudBackgroundTasks; use: () => boolean }

export class BackgroundTaskStore {
  readonly #sql: Sql
  readonly #remote: RemoteBackgroundTasks | undefined
  constructor(sql: Sql, remote?: RemoteBackgroundTasks) {
    this.#sql = sql
    this.#remote = remote
  }

  #crud(): CrudBackgroundTasks | undefined {
    return this.#remote?.use() ? this.#remote.crud : undefined
  }

  /** Inserts an active task, or returns undefined when the session already has MAX_ACTIVE. */
  async create(t: NewTask): Promise<BackgroundTask | undefined> {
    const crud = this.#crud()
    if (crud) return crud.create(t)
    return this.#sql.begin(async (tx) => {
      // One writer per session at a time, so two creates cannot both pass the count.
      await tx`SELECT pg_advisory_xact_lock(hashtext(${`bgtask:${t.sessionId}`}))`
      const [{ n }] = (await tx`
        SELECT count(*)::int AS n FROM ai_background_tasks WHERE session_id = ${t.sessionId} AND state = 'active'`) as unknown as [
        { n: number },
      ]
      if (n >= MAX_ACTIVE) return undefined
      const [row] = (await tx`
        INSERT INTO ai_background_tasks (id, session_id, kind, description, spec, created_by, next_at, expires_at)
        VALUES (${randomBytes(4).toString('hex')}, ${t.sessionId}, ${t.kind}, ${t.description}, ${tx.json(t.spec as never)},
                ${tx.json(t.createdBy as never)}, ${t.nextAt ?? null}, ${t.expiresAt ?? null})
        RETURNING *`) as unknown as Row[]
      return task(row!)
    }) as Promise<BackgroundTask | undefined>
  }

  async get(id: string): Promise<BackgroundTask | undefined> {
    const crud = this.#crud()
    if (crud) return crud.get(id)
    const [row] = await this.#sql<Row[]>`SELECT * FROM ai_background_tasks WHERE id = ${id}`
    return row ? task(row) : undefined
  }

  /** A session's tasks, newest first; the active ones only unless `all`. */
  async list(sessionId: string, options: { all?: boolean } = {}): Promise<BackgroundTask[]> {
    const crud = this.#crud()
    if (crud) return crud.list(sessionId, options)
    const rows = options.all
      ? await this.#sql<Row[]>`SELECT * FROM ai_background_tasks WHERE session_id = ${sessionId} ORDER BY created_at DESC, id`
      : await this.#sql<Row[]>`
          SELECT * FROM ai_background_tasks WHERE session_id = ${sessionId} AND state = 'active' ORDER BY created_at DESC, id`
    return rows.map(task)
  }

  /** Every session's active recurring cron jobs, for the start-time pass that makes each a Schedule (temporal.ts). */
  async activeRecurringCrons(): Promise<BackgroundTask[]> {
    const crud = this.#crud()
    if (crud) return crud.activeRecurringCrons()
    const rows = await this.#sql<Row[]>`
      SELECT * FROM ai_background_tasks
       WHERE state = 'active' AND kind = 'cron' AND coalesce(spec->>'recurring', 'true') <> 'false'
       ORDER BY created_at, id`
    return rows.map(task)
  }

  /** Records the next fire time of an active task. */
  async scheduled(id: string, nextAt: Date): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.scheduled(id, nextAt)
    await this.#sql`UPDATE ai_background_tasks SET next_at = ${nextAt}, updated_at = now() WHERE id = ${id} AND state = 'active'`
  }

  /** Counts a fire of an active task. */
  async fired(id: string, at: Date): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.fired(id, at)
    await this.#sql`
      UPDATE ai_background_tasks SET fired = fired + 1, last_fired_at = ${at}, next_at = NULL, updated_at = now()
      WHERE id = ${id} AND state = 'active'`
  }

  /** Ends an active task; false when it had ended already. */
  async finish(id: string, state: Exclude<TaskState, 'active'>, detail?: string): Promise<boolean> {
    const crud = this.#crud()
    if (crud) return crud.finish(id, state, detail)
    const rows = await this.#sql`
      UPDATE ai_background_tasks SET state = ${state}, detail = ${detail ?? null}, next_at = NULL, updated_at = now()
      WHERE id = ${id} AND state = 'active' RETURNING id`
    return rows.length === 1
  }

  /** Records one event of a monitor; false once it has MAX_EVENTS (it is then stopped). */
  async appendEvent(taskId: string, text: string): Promise<boolean> {
    const crud = this.#crud()
    if (crud) return crud.appendEvent(taskId, text)
    const rows = await this.#sql`
      INSERT INTO ai_background_events (task_id, text)
      SELECT ${taskId}, ${text.slice(0, NOTICE_MAX_CHARS)}
      WHERE (SELECT count(*) FROM ai_background_events WHERE task_id = ${taskId}) < ${MAX_EVENTS}
      RETURNING seq`
    return rows.length === 1
  }

  /** Records a monitor's last notice (its end), past MAX_EVENTS. */
  async appendNotice(taskId: string, text: string): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.appendNotice(taskId, text)
    await this.#sql`INSERT INTO ai_background_events (task_id, text) VALUES (${taskId}, ${text.slice(0, NOTICE_MAX_CHARS)})`
  }

  /**
   * A task's events not yet sent to its session, oldest first, as many as fit in
   * `maxChars` of text, and always the oldest one (a notice is at most NOTICE_MAX_CHARS).
   * Ordered by the bigint `seq`, not the text it is answered as ("10" sorts before "9"), so
   * the last one is the newest, which `delivered` marks up to.
   */
  async pendingEvents(taskId: string, maxChars = DELIVER_MAX_CHARS): Promise<{ seq: string; text: string }[]> {
    const crud = this.#crud()
    if (crud) return crud.pendingEvents(taskId, maxChars)
    return this.#sql<{ seq: string; text: string }[]>`
      SELECT seq::text AS seq, text FROM (
        SELECT seq, text,
          sum(length(text)) OVER (ORDER BY seq) AS through,
          row_number() OVER (ORDER BY seq) AS n
        FROM ai_background_events WHERE task_id = ${taskId} AND delivered_at IS NULL
      ) pending
      WHERE n = 1 OR through <= ${maxChars}
      ORDER BY pending.seq`
  }

  /** How many of a task's events are not yet sent to its session. */
  async pendingCount(taskId: string): Promise<number> {
    const crud = this.#crud()
    if (crud) return crud.eventCount(taskId, { pending: true })
    const [row] = await this.#sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM ai_background_events WHERE task_id = ${taskId} AND delivered_at IS NULL`
    return row?.n ?? 0
  }

  async delivered(taskId: string, upTo: string): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.delivered(taskId, upTo)
    await this.#sql`
      UPDATE ai_background_events SET delivered_at = now() WHERE task_id = ${taskId} AND seq <= ${upTo} AND delivered_at IS NULL`
  }

  async eventCount(taskId: string): Promise<number> {
    const crud = this.#crud()
    if (crud) return crud.eventCount(taskId)
    const [row] = await this.#sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_background_events WHERE task_id = ${taskId}`
    return row?.n ?? 0
  }

  /** Replaces a task's spec (a Workflow's, once its script is kept). */
  async setSpec(id: string, spec: Record<string, unknown>): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.setSpec(id, spec)
    await this.#sql`UPDATE ai_background_tasks SET spec = ${this.#sql.json(spec as never)}, updated_at = now() WHERE id = ${id}`
  }

  /** A running task's latest progress (a Workflow's phase or log line), shown in the panel. */
  async progress(id: string, detail: string): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.progress(id, detail)
    await this.#sql`UPDATE ai_background_tasks SET detail = ${detail}, updated_at = now() WHERE id = ${id} AND state = 'active'`
  }

  /** A Workflow's journalled agent() result (ai_workflow_calls), or undefined. */
  async journalled(taskId: string, key: string, occurrence: number): Promise<{ result: unknown; ok: boolean } | undefined> {
    const crud = this.#crud()
    if (crud) return crud.journalled(taskId, key, occurrence)
    const [row] = await this.#sql<{ result: { value: unknown }; ok: boolean }[]>`
      SELECT result, ok FROM ai_workflow_calls WHERE task_id = ${taskId} AND call_key = ${key} AND occurrence = ${occurrence}`
    // Kept as {value}, so a null result is a row too.
    return row ? { result: row.result.value, ok: row.ok } : undefined
  }

  /** Records a child chat a Workflow's run started (#3395). */
  async recordChild(taskId: string, childId: string): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.recordChild(taskId, childId)
    await this.#sql`INSERT INTO ai_workflow_children (task_id, child_id) VALUES (${taskId}, ${childId}) ON CONFLICT DO NOTHING`
  }

  /** The child chats a Workflow's earlier attempts left open: not done, and named by no journal row. */
  async openChildren(taskId: string): Promise<string[]> {
    const crud = this.#crud()
    if (crud) return crud.openChildren(taskId)
    const rows = await this.#sql<{ child_id: string }[]>`
      SELECT c.child_id FROM ai_workflow_children c
        JOIN ai_sessions s ON s.id = c.child_id AND s.status <> 'done'
       WHERE c.task_id = ${taskId}
         AND NOT EXISTS (SELECT 1 FROM ai_workflow_calls w WHERE w.task_id = c.task_id AND w.child_id = c.child_id)
       ORDER BY c.created_at`
    return rows.map((r) => r.child_id)
  }

  /** Records one agent() result in a Workflow's journal; a second write of the same call keeps the first. */
  async journal(taskId: string, key: string, occurrence: number, entry: { result: unknown; ok: boolean; childId?: string | undefined }): Promise<void> {
    const crud = this.#crud()
    if (crud) return crud.journal(taskId, key, occurrence, entry)
    await this.#sql`
      INSERT INTO ai_workflow_calls (task_id, call_key, occurrence, result, ok, child_id)
      VALUES (${taskId}, ${key}, ${occurrence}, ${this.#sql.json({ value: entry.result } as never)}, ${entry.ok}, ${entry.childId ?? null})
      ON CONFLICT DO NOTHING`
  }
}

/** A row of `ai_background_tasks` as the routes answer it (backend api/crud_agent_tasks.py `Task`). */
type WireTask = Omit<BackgroundTask, 'sessionId' | 'createdBy' | 'nextAt' | 'lastFiredAt' | 'expiresAt' | 'createdAt' | 'updatedAt'> & {
  session_id: string
  created_by: Owner
  next_at: string | null
  last_fired_at: string | null
  expires_at: string | null
  created_at: string
  updated_at: string
}

const date = (value: string | null): Date | null => (value === null ? null : new Date(value))

const fromWire = (w: WireTask): BackgroundTask => ({
  id: w.id,
  sessionId: w.session_id,
  kind: w.kind,
  description: w.description,
  spec: w.spec,
  createdBy: w.created_by,
  state: w.state,
  nextAt: date(w.next_at),
  fired: w.fired,
  lastFiredAt: date(w.last_fired_at),
  expiresAt: date(w.expires_at),
  detail: w.detail,
  createdAt: new Date(w.created_at),
  updatedAt: new Date(w.updated_at),
})

/** `BackgroundTaskStore`'s methods over the backend's CRUD routes (`…/crud/agent/background-tasks`), one transaction each. */
/** A task's id as the table's CHECK has it (`randomBytes(4)` in hex). */
const TASK_ID = /^[0-9a-f]{8}$/

/**
 * Whether `id` can name a task. The routes refuse any other id (422), where the SQL found no row:
 * a model's or a browser's id (a Workflow's `wf_<id>`, a shell id, none at all) reads as no task
 * here too, so `TaskStop` still answers "No task found" and the panel's cancel 404, without a call.
 * A write that inserts (an event, a journal row, a child) is left to the route: on SQL it failed
 * too, on the foreign key.
 */
function isTaskId(id: string): boolean {
  return TASK_ID.test(id)
}

export class CrudBackgroundTasks {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  #get<T>(step: string, path: string): Promise<T> {
    return crudRead<T>(this.#fetch, this.#base, step, `/background-tasks${path}`)
  }

  #post<T>(step: string, path: string, body: unknown): Promise<T> {
    return crudRead<T>(this.#fetch, this.#base, step, `/background-tasks${path}`, { method: 'POST', body: wellFormed(body) })
  }

  async create(t: NewTask): Promise<BackgroundTask | undefined> {
    const body = {
      session_id: t.sessionId,
      kind: t.kind,
      description: t.description,
      spec: t.spec,
      created_by: t.createdBy,
      next_at: t.nextAt?.toISOString() ?? null,
      expires_at: t.expiresAt?.toISOString() ?? null,
    }
    const { task } = await this.#post<{ task: WireTask | null }>('a background task created', '', body)
    return task ? fromWire(task) : undefined
  }

  async get(id: string): Promise<BackgroundTask | undefined> {
    if (!isTaskId(id)) return undefined
    const { task } = await this.#get<{ task: WireTask | null }>('a background task', `/${encodeURIComponent(id)}`)
    return task ? fromWire(task) : undefined
  }

  async list(sessionId: string, options: { all?: boolean } = {}): Promise<BackgroundTask[]> {
    const params = new URLSearchParams({ session_id: sessionId, ...(options.all ? { all: 'true' } : {}) })
    const { tasks } = await this.#get<{ tasks: WireTask[] }>("a session's background tasks", `?${params.toString()}`)
    return tasks.map(fromWire)
  }

  async activeRecurringCrons(): Promise<BackgroundTask[]> {
    const { tasks } = await this.#get<{ tasks: WireTask[] }>('the recurring cron jobs', '/recurring-crons')
    return tasks.map(fromWire)
  }

  async scheduled(id: string, nextAt: Date): Promise<void> {
    if (!isTaskId(id)) return
    await this.#post('a background task scheduled', `/${encodeURIComponent(id)}/scheduled`, { at: nextAt.toISOString() })
  }

  async fired(id: string, at: Date): Promise<void> {
    if (!isTaskId(id)) return
    await this.#post('a background task fired', `/${encodeURIComponent(id)}/fired`, { at: at.toISOString() })
  }

  async finish(id: string, state: Exclude<TaskState, 'active'>, detail?: string): Promise<boolean> {
    if (!isTaskId(id)) return false
    const body = { state, detail: detail ?? null }
    return (await this.#post<{ finished: boolean }>('a background task ended', `/${encodeURIComponent(id)}/finish`, body)).finished
  }

  async appendEvent(taskId: string, text: string): Promise<boolean> {
    const body = { text: text.slice(0, NOTICE_MAX_CHARS) }
    return (await this.#post<{ appended: boolean }>("a monitor's event", `/${encodeURIComponent(taskId)}/events`, body)).appended
  }

  async appendNotice(taskId: string, text: string): Promise<void> {
    const body = { text: text.slice(0, NOTICE_MAX_CHARS), notice: true }
    await this.#post("a monitor's notice", `/${encodeURIComponent(taskId)}/events`, body)
  }

  async pendingEvents(taskId: string, maxChars = DELIVER_MAX_CHARS): Promise<{ seq: string; text: string }[]> {
    if (!isTaskId(taskId)) return []
    const params = new URLSearchParams({ max_chars: String(maxChars) })
    const { events } = await this.#get<{ events: { seq: string; text: string }[] }>(
      "a monitor's pending events",
      `/${encodeURIComponent(taskId)}/events/pending?${params.toString()}`,
    )
    return events
  }

  /** How many events a task has; only those not yet sent to its session with `pending`. */
  async eventCount(taskId: string, options: { pending?: boolean } = {}): Promise<number> {
    if (!isTaskId(taskId)) return 0
    const query = options.pending ? '?pending=true' : ''
    return (await this.#get<{ count: number }>("a monitor's event count", `/${encodeURIComponent(taskId)}/events/count${query}`)).count
  }

  async delivered(taskId: string, upTo: string): Promise<void> {
    if (!isTaskId(taskId)) return
    await this.#post("a monitor's events delivered", `/${encodeURIComponent(taskId)}/events/delivered`, { up_to: upTo })
  }

  async setSpec(id: string, spec: Record<string, unknown>): Promise<void> {
    if (!isTaskId(id)) return
    await this.#post("a background task's spec", `/${encodeURIComponent(id)}/spec`, { spec })
  }

  async progress(id: string, detail: string): Promise<void> {
    if (!isTaskId(id)) return
    await this.#post("a background task's progress", `/${encodeURIComponent(id)}/progress`, { detail })
  }

  async journalled(taskId: string, key: string, occurrence: number): Promise<{ result: unknown; ok: boolean } | undefined> {
    if (!isTaskId(taskId)) return undefined
    const params = new URLSearchParams({ key, occurrence: String(occurrence) })
    const { call } = await this.#get<{ call: { result: unknown; ok: boolean } | null }>(
      "a Workflow's journalled call",
      `/${encodeURIComponent(taskId)}/calls?${params.toString()}`,
    )
    return call ?? undefined
  }

  async journal(taskId: string, key: string, occurrence: number, entry: { result: unknown; ok: boolean; childId?: string | undefined }): Promise<void> {
    // JSON has no undefined, so an undefined result is sent, and kept, as null: `{"value": null}`
    // where the SQL kept `{}`. A replay reads `.value` from either as nothing (null, or undefined).
    const body = { key, occurrence, result: entry.result ?? null, ok: entry.ok, child_id: entry.childId ?? null }
    await this.#post("a Workflow's call journalled", `/${encodeURIComponent(taskId)}/calls`, body)
  }

  async recordChild(taskId: string, childId: string): Promise<void> {
    await this.#post("a Workflow's child chat", `/${encodeURIComponent(taskId)}/children`, { child_id: childId })
  }

  async openChildren(taskId: string): Promise<string[]> {
    if (!isTaskId(taskId)) return []
    return (await this.#get<{ children: string[] }>("a Workflow's open child chats", `/${encodeURIComponent(taskId)}/children/open`)).children
  }
}
