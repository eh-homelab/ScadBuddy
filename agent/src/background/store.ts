import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import type { Owner } from '../sessions/protocol.js'

// A session's background tasks (ai_background_tasks, migration
// 20261013T0445Z_background_tasks.sql): what CronCreate, ScheduleWakeup, Monitor and
// Workflow left running past their turn. The store is the record the panel lists; each
// active row's workflow (temporal/backgroundTask.ts) is what fires it.

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

export class BackgroundTaskStore {
  readonly #sql: Sql
  constructor(sql: Sql) {
    this.#sql = sql
  }

  /** Inserts an active task, or returns undefined when the session already has MAX_ACTIVE. */
  async create(t: NewTask): Promise<BackgroundTask | undefined> {
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
    const [row] = await this.#sql<Row[]>`SELECT * FROM ai_background_tasks WHERE id = ${id}`
    return row ? task(row) : undefined
  }

  /** A session's tasks, newest first; the active ones only unless `all`. */
  async list(sessionId: string, options: { all?: boolean } = {}): Promise<BackgroundTask[]> {
    const rows = options.all
      ? await this.#sql<Row[]>`SELECT * FROM ai_background_tasks WHERE session_id = ${sessionId} ORDER BY created_at DESC, id`
      : await this.#sql<Row[]>`
          SELECT * FROM ai_background_tasks WHERE session_id = ${sessionId} AND state = 'active' ORDER BY created_at DESC, id`
    return rows.map(task)
  }

  /** Records the next fire time of an active task. */
  async scheduled(id: string, nextAt: Date): Promise<void> {
    await this.#sql`UPDATE ai_background_tasks SET next_at = ${nextAt}, updated_at = now() WHERE id = ${id} AND state = 'active'`
  }

  /** Counts a fire of an active task. */
  async fired(id: string, at: Date): Promise<void> {
    await this.#sql`
      UPDATE ai_background_tasks SET fired = fired + 1, last_fired_at = ${at}, next_at = NULL, updated_at = now()
      WHERE id = ${id} AND state = 'active'`
  }

  /** Ends an active task; false when it had ended already. */
  async finish(id: string, state: Exclude<TaskState, 'active'>, detail?: string): Promise<boolean> {
    const rows = await this.#sql`
      UPDATE ai_background_tasks SET state = ${state}, detail = ${detail ?? null}, next_at = NULL, updated_at = now()
      WHERE id = ${id} AND state = 'active' RETURNING id`
    return rows.length === 1
  }

  /** Records one event of a monitor; false once it has MAX_EVENTS (it is then stopped). */
  async appendEvent(taskId: string, text: string): Promise<boolean> {
    const rows = await this.#sql`
      INSERT INTO ai_background_events (task_id, text)
      SELECT ${taskId}, ${text.slice(0, NOTICE_MAX_CHARS)}
      WHERE (SELECT count(*) FROM ai_background_events WHERE task_id = ${taskId}) < ${MAX_EVENTS}
      RETURNING seq`
    return rows.length === 1
  }

  /** Records a monitor's last notice (its end), past MAX_EVENTS. */
  async appendNotice(taskId: string, text: string): Promise<void> {
    await this.#sql`INSERT INTO ai_background_events (task_id, text) VALUES (${taskId}, ${text.slice(0, NOTICE_MAX_CHARS)})`
  }

  /**
   * A task's events not yet sent to its session, oldest first, as many as fit in
   * `maxChars` of text, and always the oldest one (a notice is at most NOTICE_MAX_CHARS).
   */
  async pendingEvents(taskId: string, maxChars = DELIVER_MAX_CHARS): Promise<{ seq: string; text: string }[]> {
    return this.#sql<{ seq: string; text: string }[]>`
      SELECT seq::text AS seq, text FROM (
        SELECT seq, text,
          sum(length(text)) OVER (ORDER BY seq) AS through,
          row_number() OVER (ORDER BY seq) AS n
        FROM ai_background_events WHERE task_id = ${taskId} AND delivered_at IS NULL
      ) pending
      WHERE n = 1 OR through <= ${maxChars}
      ORDER BY seq`
  }

  /** How many of a task's events are not yet sent to its session. */
  async pendingCount(taskId: string): Promise<number> {
    const [row] = await this.#sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM ai_background_events WHERE task_id = ${taskId} AND delivered_at IS NULL`
    return row?.n ?? 0
  }

  async delivered(taskId: string, upTo: string): Promise<void> {
    await this.#sql`
      UPDATE ai_background_events SET delivered_at = now() WHERE task_id = ${taskId} AND seq <= ${upTo} AND delivered_at IS NULL`
  }

  async eventCount(taskId: string): Promise<number> {
    const [row] = await this.#sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_background_events WHERE task_id = ${taskId}`
    return row?.n ?? 0
  }

  /** Replaces a task's spec (a Workflow's, once its script is kept). */
  async setSpec(id: string, spec: Record<string, unknown>): Promise<void> {
    await this.#sql`UPDATE ai_background_tasks SET spec = ${this.#sql.json(spec as never)}, updated_at = now() WHERE id = ${id}`
  }

  /** A running task's latest progress (a Workflow's phase or log line), shown in the panel. */
  async progress(id: string, detail: string): Promise<void> {
    await this.#sql`UPDATE ai_background_tasks SET detail = ${detail}, updated_at = now() WHERE id = ${id} AND state = 'active'`
  }

  /** A Workflow's journalled agent() result (ai_workflow_calls), or undefined. */
  async journalled(taskId: string, key: string, occurrence: number): Promise<{ result: unknown; ok: boolean } | undefined> {
    const [row] = await this.#sql<{ result: { value: unknown }; ok: boolean }[]>`
      SELECT result, ok FROM ai_workflow_calls WHERE task_id = ${taskId} AND call_key = ${key} AND occurrence = ${occurrence}`
    // Kept as {value}, so a null result is a row too.
    return row ? { result: row.result.value, ok: row.ok } : undefined
  }

  /** Records one agent() result in a Workflow's journal; a second write of the same call keeps the first. */
  async journal(taskId: string, key: string, occurrence: number, entry: { result: unknown; ok: boolean; childId?: string | undefined }): Promise<void> {
    await this.#sql`
      INSERT INTO ai_workflow_calls (task_id, call_key, occurrence, result, ok, child_id)
      VALUES (${taskId}, ${key}, ${occurrence}, ${this.#sql.json({ value: entry.result } as never)}, ${entry.ok}, ${entry.childId ?? null})
      ON CONFLICT DO NOTHING`
  }
}
