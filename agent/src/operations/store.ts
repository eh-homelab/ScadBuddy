import { createHash, randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { crudRead, wellFormed } from '../api/crudRead.js'
import { PG_CHANNEL } from '../events/bus.js'
import type { OperationActor } from '../temporal/names.js'

// `ai_operations` (spec 2026-10-01 §4.2 "Our record", #1055): one row per execution of
// an AgentOperation workflow, written only by its activities, as the backend's
// `operations` (backend/scadbuddy/operations/store.py) is by its `Operation`'s.
//
// Since #5299 step 8 the API owns the table: once `GET …/crud/agent/areas` lists `operations`
// (api/crudAreas.ts), every method goes through `CrudOperations`, the backend's
// api/crud_agent_tasks.py routes, which write the operation's activity row in the same
// transaction; until then, its own SQL, which announces each change on the bus for the
// backend's activity index to read (`announce`).

export type OperationStatus = 'running' | 'succeeded' | 'failed'

/** The problem a failed operation's route answers with. */
export type OperationError = {
  status: number
  title: string
  detail: string
  type?: string
  extensions?: Record<string, unknown>
}

export type Operation = {
  id: string
  kind: string
  subject: string
  status: OperationStatus
  /** The route's answer body, once `succeeded`. */
  result: unknown
  error: OperationError | null
  created_at: string
  finished_at: string | null
  /** Only on an answer: an earlier command with the same key, so this request ran nothing. */
  repeated?: boolean
}

/** Finished operations are kept this long: the namespace's 168 h (spec §3.3). */
export const OPERATION_RETENTION_SECONDS = 7 * 24 * 3600

/** The kind, its subject, the canonical body and the client's key (§4.2 step 1), as the backend's `operation_key`. */
export function operationKey(kind: string, subject: string, request: unknown, requestId: string): string {
  return createHash('sha256').update(`${kind}\n${subject}\n${canonicalJson(request)}\n${requestId}`).digest('hex')
}

/** JSON with sorted keys and no spaces: Python's `json.dumps(sort_keys=True, separators=(",", ":"))`. */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

type Row = {
  id: string
  kind: string
  subject: string
  status: OperationStatus
  result: unknown
  error: OperationError | null
  created_at: Date
  finished_at: Date | null
}

const COLUMNS = 'id, kind, subject, status, result, error, created_at, finished_at'

function view(row: Row): Operation {
  return {
    id: row.id,
    kind: row.kind,
    subject: row.subject,
    status: row.status,
    result: row.result,
    error: row.error,
    created_at: row.created_at.toISOString(),
    finished_at: row.finished_at?.toISOString() ?? null,
  }
}

export type InsertOperation = {
  id: string
  kind: string
  subject: string
  operationKey: string
  request: unknown
  workflowId: string
  workflowRunId: string
  actor?: OperationActor
}

/** Tell the bus a row changed (the backend's activity index re-reads it); in the writing transaction. */
async function announce(tx: TransactionSql, operationId: string): Promise<void> {
  const event = {
    id: randomUUID().replaceAll('-', ''),
    at: new Date().toISOString(),
    kind: 'ai_operation.changed',
    operation_id: operationId,
  }
  await tx`SELECT pg_notify(${PG_CHANNEL}, ${JSON.stringify(event)})`
}

/** The CRUD routes while the API serves them (`use`), else the store's own SQL. */
export type RemoteOperations = { crud: CrudOperations; use: () => boolean }

export class OperationStore {
  private readonly sql: Sql
  private readonly remote: RemoteOperations | undefined

  constructor(sql: Sql, remote?: RemoteOperations) {
    this.sql = sql
    this.remote = remote
  }

  private crud(): CrudOperations | undefined {
    return this.remote?.use() ? this.remote.crud : undefined
  }

  /** The key's newest operation, whatever its status: one key is one effect. */
  async find(operationKey: string): Promise<Operation | undefined> {
    const crud = this.crud()
    if (crud) return (await crud.find(operationKey))?.operation
    const [row] = await this.sql.unsafe<Row[]>(
      `SELECT ${COLUMNS} FROM ai_operations WHERE operation_key = $1 ORDER BY created_at DESC LIMIT 1`,
      [operationKey],
    )
    return row ? view(row) : undefined
  }

  async get(id: string): Promise<Operation | undefined> {
    const crud = this.crud()
    if (crud) return (await crud.get(id))?.operation
    const [row] = await this.sql.unsafe<Row[]>(`SELECT ${COLUMNS} FROM ai_operations WHERE id = $1`, [id])
    return row ? view(row) : undefined
  }

  /** The execution that runs (or ran) an operation. */
  async execution(id: string): Promise<{ workflowId: string; workflowRunId: string } | undefined> {
    const crud = this.crud()
    if (crud) return (await crud.get(id))?.execution
    const [row] = await this.sql<{ workflow_id: string; workflow_run_id: string }[]>`
      SELECT workflow_id, workflow_run_id FROM ai_operations WHERE id = ${id}`
    return row ? { workflowId: row.workflow_id, workflowRunId: row.workflow_run_id } : undefined
  }

  /**
   * Record an accepted operation, or return the execution's row when it has one (a
   * retried activity). Prunes operations finished more than the retention ago.
   */
  async insert(op: InsertOperation, retentionSeconds = OPERATION_RETENTION_SECONDS): Promise<Operation> {
    const crud = this.crud()
    if (crud) return crud.insert(op, retentionSeconds)
    return this.sql.begin(async (tx) => {
      await tx`DELETE FROM ai_operations WHERE finished_at < now() - make_interval(secs => ${retentionSeconds})`
      const [inserted] = await tx<Row[]>`
        INSERT INTO ai_operations (id, kind, subject, operation_key, status, request, workflow_id, workflow_run_id,
                                   principal, session, via_tool)
        VALUES (${op.id}, ${op.kind}, ${op.subject}, ${op.operationKey}, 'running',
                ${tx.json((op.request ?? null) as never)}, ${op.workflowId}, ${op.workflowRunId},
                ${op.actor?.principal ?? null}, ${op.actor?.session ?? null}, ${op.actor?.viaTool ?? null})
        ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING
        RETURNING id, kind, subject, status, result, error, created_at, finished_at`
      if (inserted) {
        await announce(tx, inserted.id)
        return view(inserted)
      }
      const [existing] = await tx.unsafe<Row[]>(
        `SELECT ${COLUMNS} FROM ai_operations WHERE workflow_id = $1 AND workflow_run_id = $2`,
        [op.workflowId, op.workflowRunId],
      )
      return view(existing!)
    })
  }

  /** End a running operation; a retried end finds it ended and changes nothing. */
  async finish(id: string, outcome: { result: unknown } | { error: OperationError }): Promise<Operation> {
    const crud = this.crud()
    if (crud) return crud.finish(id, outcome)
    const failed = 'error' in outcome
    const result = failed ? null : this.sql.json((outcome.result ?? null) as never)
    const error = failed ? this.sql.json(outcome.error as never) : null
    await this.sql.begin(async (tx) => {
      const ended = await tx`
        UPDATE ai_operations SET status = ${failed ? 'failed' : 'succeeded'}, result = ${result}, error = ${error},
                                 finished_at = now()
        WHERE id = ${id} AND status = 'running'
        RETURNING id`
      if (ended.length > 0) await announce(tx, id)
    })
    const op = await this.get(id)
    if (!op) throw new Error(`no operation ${id}`)
    return op
  }
}

/** A row of `ai_operations` as the routes answer it (backend api/crud_agent_tasks.py `Operation`). */
type WireOperation = Omit<Operation, 'repeated'> & { workflow_id: string; workflow_run_id: string }

type Execution = { workflowId: string; workflowRunId: string }

/** The operation, with its times as `view` gives them, and the execution that runs it. */
function fromWire(w: WireOperation): { operation: Operation; execution: Execution } {
  return {
    operation: {
      id: w.id,
      kind: w.kind,
      subject: w.subject,
      status: w.status,
      result: w.result,
      error: w.error,
      created_at: new Date(w.created_at).toISOString(),
      finished_at: w.finished_at === null ? null : new Date(w.finished_at).toISOString(),
    },
    execution: { workflowId: w.workflow_id, workflowRunId: w.workflow_run_id },
  }
}

/** `OperationStore`'s methods over the backend's CRUD routes (`…/crud/agent/operations`), one transaction each. */
export class CrudOperations {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  async #one(step: string, path: string, body?: unknown): Promise<WireOperation | null> {
    const init = body === undefined ? {} : { method: 'POST' as const, body: wellFormed(body) }
    return (await crudRead<{ operation: WireOperation | null }>(this.#fetch, this.#base, step, `/operations${path}`, init)).operation
  }

  async find(operationKey: string): Promise<{ operation: Operation; execution: Execution } | undefined> {
    const w = await this.#one("an operation's key", `/by-key/${encodeURIComponent(operationKey)}`)
    return w ? fromWire(w) : undefined
  }

  async get(id: string): Promise<{ operation: Operation; execution: Execution } | undefined> {
    const w = await this.#one('an operation', `/${encodeURIComponent(id)}`)
    return w ? fromWire(w) : undefined
  }

  async insert(op: InsertOperation, retentionSeconds = OPERATION_RETENTION_SECONDS): Promise<Operation> {
    const actor = op.actor ? { principal: op.actor.principal, session: op.actor.session ?? null, via_tool: op.actor.viaTool ?? null } : null
    const body = {
      id: op.id,
      kind: op.kind,
      subject: op.subject,
      operation_key: op.operationKey,
      request: op.request ?? null,
      workflow_id: op.workflowId,
      workflow_run_id: op.workflowRunId,
      actor,
      retention_seconds: retentionSeconds,
    }
    const w = await this.#one('an operation recorded', '', body)
    if (!w) throw new Error(`no operation ${op.id}`)
    return fromWire(w).operation
  }

  async finish(id: string, outcome: { result: unknown } | { error: OperationError }): Promise<Operation> {
    const body = 'error' in outcome ? { error: outcome.error } : { result: outcome.result ?? null }
    const w = await this.#one('an operation ended', `/${encodeURIComponent(id)}/finish`, body)
    if (!w) throw new Error(`no operation ${id}`)
    return fromWire(w).operation
  }
}
