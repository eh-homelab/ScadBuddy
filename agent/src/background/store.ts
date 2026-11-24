import { crudRead, wellFormed } from '../api/crudRead.js'
import type { Owner } from '../sessions/protocol.js'

// A session's background tasks (ai_background_tasks, migration
// 20261013T0445Z_background_tasks.sql): what CronCreate, ScheduleWakeup, Monitor and
// Workflow left running past their turn. The store is the record the panel lists; each
// active row's workflow (temporal/backgroundTask.ts) is what fires it.
//
// Since #5299 step 8 the API owns these tables: every method goes through `CrudBackgroundTasks`,
// the backend's api/crud_agent_tasks.py routes, one transaction each.

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
 * would be ~3 MB in one message, past any context, and a session would then
 * fail every turn after it on the same transcript. The rest go in the next delivery.
 */
export const DELIVER_MAX_CHARS = 64_000

export type NewTask = {
  sessionId: string
  kind: TaskKind
  description: string
  spec: Record<string, unknown>
  createdBy: Owner
  nextAt?: Date | undefined
  expiresAt?: Date | undefined
}

/** What `BackgroundTaskStore` reads and writes through: the API's CRUD routes (`CrudBackgroundTasks`). */
export type BackgroundTaskRows = Pick<
  CrudBackgroundTasks,
  | 'create'
  | 'get'
  | 'list'
  | 'activeRecurringCrons'
  | 'scheduled'
  | 'fired'
  | 'finish'
  | 'appendEvent'
  | 'appendNotice'
  | 'pendingEvents'
  | 'eventCount'
  | 'delivered'
  | 'setSpec'
  | 'progress'
  | 'journalled'
  | 'recordChild'
  | 'openChildren'
  | 'journal'
>

/** A session's background tasks, a Monitor's events and a Workflow's journal, through the API (#5299 step 8). */
export class BackgroundTaskStore {
  readonly #rows: BackgroundTaskRows
  constructor(rows: BackgroundTaskRows) {
    this.#rows = rows
  }

  /** Inserts an active task, or returns undefined when the session already has MAX_ACTIVE. */
  create(t: NewTask): Promise<BackgroundTask | undefined> {
    return this.#rows.create(t)
  }

  get(id: string): Promise<BackgroundTask | undefined> {
    return this.#rows.get(id)
  }

  /** A session's tasks, newest first; the active ones only unless `all`. */
  list(sessionId: string, options: { all?: boolean } = {}): Promise<BackgroundTask[]> {
    return this.#rows.list(sessionId, options)
  }

  /** Every session's active recurring cron jobs, for the start-time pass that makes each a Schedule (temporal.ts). */
  activeRecurringCrons(): Promise<BackgroundTask[]> {
    return this.#rows.activeRecurringCrons()
  }

  /** Records the next fire time of an active task. */
  scheduled(id: string, nextAt: Date): Promise<void> {
    return this.#rows.scheduled(id, nextAt)
  }

  /** Counts a fire of an active task. */
  fired(id: string, at: Date): Promise<void> {
    return this.#rows.fired(id, at)
  }

  /** Ends an active task; false when it had ended already. */
  finish(id: string, state: Exclude<TaskState, 'active'>, detail?: string): Promise<boolean> {
    return this.#rows.finish(id, state, detail)
  }

  /** Records one event of a monitor; false once it has MAX_EVENTS (it is then stopped). */
  appendEvent(taskId: string, text: string): Promise<boolean> {
    return this.#rows.appendEvent(taskId, text)
  }

  /** Records a monitor's last notice (its end), past MAX_EVENTS. */
  appendNotice(taskId: string, text: string): Promise<void> {
    return this.#rows.appendNotice(taskId, text)
  }

  /**
   * A task's events not yet sent to its session, oldest first, as many as fit in
   * `maxChars` of text, and always the oldest one (a notice is at most NOTICE_MAX_CHARS).
   * Ordered by the bigint `seq`, so the last one is the newest, which `delivered` marks up to.
   */
  pendingEvents(taskId: string, maxChars = DELIVER_MAX_CHARS): Promise<{ seq: string; text: string }[]> {
    return this.#rows.pendingEvents(taskId, maxChars)
  }

  /** How many of a task's events are not yet sent to its session. */
  pendingCount(taskId: string): Promise<number> {
    return this.#rows.eventCount(taskId, { pending: true })
  }

  delivered(taskId: string, upTo: string): Promise<void> {
    return this.#rows.delivered(taskId, upTo)
  }

  eventCount(taskId: string): Promise<number> {
    return this.#rows.eventCount(taskId)
  }

  /** Replaces a task's spec (a Workflow's, once its script is kept). */
  setSpec(id: string, spec: Record<string, unknown>): Promise<void> {
    return this.#rows.setSpec(id, spec)
  }

  /** A running task's latest progress (a Workflow's phase or log line), shown in the panel. */
  progress(id: string, detail: string): Promise<void> {
    return this.#rows.progress(id, detail)
  }

  /** A Workflow's journalled agent() result (ai_workflow_calls), or undefined. */
  journalled(taskId: string, key: string, occurrence: number): Promise<{ result: unknown; ok: boolean } | undefined> {
    return this.#rows.journalled(taskId, key, occurrence)
  }

  /** Records a child chat a Workflow's run started (#3395). */
  recordChild(taskId: string, childId: string): Promise<void> {
    return this.#rows.recordChild(taskId, childId)
  }

  /** The child chats a Workflow's earlier attempts left open: not done, and named by no journal row. */
  openChildren(taskId: string): Promise<string[]> {
    return this.#rows.openChildren(taskId)
  }

  /** Records one agent() result in a Workflow's journal; a second write of the same call keeps the first. */
  journal(taskId: string, key: string, occurrence: number, entry: { result: unknown; ok: boolean; childId?: string | undefined }): Promise<void> {
    return this.#rows.journal(taskId, key, occurrence, entry)
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
 * Whether `id` can name a task. The routes refuse any other id (422):
 * a model's or a browser's id (a Workflow's `wf_<id>`, a shell id, none at all) reads as no task
 * here too, so `TaskStop` still answers "No task found" and the panel's cancel 404, without a call.
 * A write that inserts (an event, a journal row, a child) is left to the route.
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
    // JSON has no undefined, so an undefined result is sent, and kept, as null: `{"value": null}`.
    // A replay reads `.value` as nothing.
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
