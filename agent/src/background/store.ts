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
}
