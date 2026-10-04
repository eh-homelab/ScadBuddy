import { createHash } from 'node:crypto'
import type { Sql } from 'postgres'

// `ai_operations` (spec 2026-10-01 §4.2 "Our record", #1055): one row per execution of
// an AgentOperation workflow, written only by its activities, as the backend's
// `operations` (backend/scadbuddy/operations/store.py) is by its `Operation`'s.

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
}

export class OperationStore {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  /** The key's newest operation, whatever its status: one key is one effect. */
  async find(operationKey: string): Promise<Operation | undefined> {
    const [row] = await this.sql.unsafe<Row[]>(
      `SELECT ${COLUMNS} FROM ai_operations WHERE operation_key = $1 ORDER BY created_at DESC LIMIT 1`,
      [operationKey],
    )
    return row ? view(row) : undefined
  }

  async get(id: string): Promise<Operation | undefined> {
    const [row] = await this.sql.unsafe<Row[]>(`SELECT ${COLUMNS} FROM ai_operations WHERE id = $1`, [id])
    return row ? view(row) : undefined
  }

  /**
   * Record an accepted operation, or return the execution's row when it has one (a
   * retried activity). Prunes operations finished more than the retention ago.
   */
  async insert(op: InsertOperation, retentionSeconds = OPERATION_RETENTION_SECONDS): Promise<Operation> {
    return this.sql.begin(async (tx) => {
      await tx`DELETE FROM ai_operations WHERE finished_at < now() - make_interval(secs => ${retentionSeconds})`
      const [inserted] = await tx<Row[]>`
        INSERT INTO ai_operations (id, kind, subject, operation_key, status, request, workflow_id, workflow_run_id)
        VALUES (${op.id}, ${op.kind}, ${op.subject}, ${op.operationKey}, 'running',
                ${tx.json((op.request ?? null) as never)}, ${op.workflowId}, ${op.workflowRunId})
        ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING
        RETURNING id, kind, subject, status, result, error, created_at, finished_at`
      if (inserted) return view(inserted)
      const [existing] = await tx.unsafe<Row[]>(
        `SELECT ${COLUMNS} FROM ai_operations WHERE workflow_id = $1 AND workflow_run_id = $2`,
        [op.workflowId, op.workflowRunId],
      )
      return view(existing!)
    })
  }

  /** End a running operation; a retried end finds it ended and changes nothing. */
  async finish(id: string, outcome: { result: unknown } | { error: OperationError }): Promise<Operation> {
    const failed = 'error' in outcome
    const result = failed ? null : this.sql.json((outcome.result ?? null) as never)
    const error = failed ? this.sql.json(outcome.error as never) : null
    await this.sql`
      UPDATE ai_operations SET status = ${failed ? 'failed' : 'succeeded'}, result = ${result}, error = ${error},
                               finished_at = now()
      WHERE id = ${id} AND status = 'running'`
    const op = await this.get(id)
    if (!op) throw new Error(`no operation ${id}`)
    return op
  }
}
