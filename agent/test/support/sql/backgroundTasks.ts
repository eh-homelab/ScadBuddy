import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import type { Owner } from '../../../src/sessions/protocol.js'
import {
  type BackgroundTask,
  type BackgroundTaskRows,
  DELIVER_MAX_CHARS,
  MAX_ACTIVE,
  MAX_EVENTS,
  type NewTask,
  NOTICE_MAX_CHARS,
  type TaskKind,
  type TaskState,
} from '../../../src/background/store.js'

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

/** `BackgroundTaskRows` over SQL on the test database: the API's `…/background-tasks` routes, as the agent once ran them. */
export class SqlBackgroundTasks implements BackgroundTaskRows {
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

  /** Every session's active recurring cron jobs, for the start-time pass that makes each a Schedule (temporal.ts). */
  async activeRecurringCrons(): Promise<BackgroundTask[]> {
    const rows = await this.#sql<Row[]>`
      SELECT * FROM ai_background_tasks
       WHERE state = 'active' AND kind = 'cron' AND coalesce(spec->>'recurring', 'true') <> 'false'
       ORDER BY created_at, id`
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
   * Ordered by the bigint `seq`, not the text it is answered as ("10" sorts before "9"), so
   * the last one is the newest, which `delivered` marks up to.
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
      ORDER BY pending.seq`
  }

  async eventCount(taskId: string, options: { pending?: boolean } = {}): Promise<number> {
    if (!options.pending) {
      const [all] = await this.#sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_background_events WHERE task_id = ${taskId}`
      return all?.n ?? 0
    }
    const [row] = await this.#sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM ai_background_events WHERE task_id = ${taskId} AND delivered_at IS NULL`
    return row?.n ?? 0
  }

  async delivered(taskId: string, upTo: string): Promise<void> {
    await this.#sql`
      UPDATE ai_background_events SET delivered_at = now() WHERE task_id = ${taskId} AND seq <= ${upTo} AND delivered_at IS NULL`
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

  /** Records a child chat a Workflow's run started (#3395). */
  async recordChild(taskId: string, childId: string): Promise<void> {
    await this.#sql`INSERT INTO ai_workflow_children (task_id, child_id) VALUES (${taskId}, ${childId}) ON CONFLICT DO NOTHING`
  }

  /** The child chats a Workflow's earlier attempts left open: not done, and named by no journal row. */
  async openChildren(taskId: string): Promise<string[]> {
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
    await this.#sql`
      INSERT INTO ai_workflow_calls (task_id, call_key, occurrence, result, ok, child_id)
      VALUES (${taskId}, ${key}, ${occurrence}, ${this.#sql.json({ value: entry.result } as never)}, ${entry.ok}, ${entry.childId ?? null})
      ON CONFLICT DO NOTHING`
  }
}

