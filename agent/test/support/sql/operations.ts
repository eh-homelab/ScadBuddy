import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { PG_CHANNEL } from '../../../src/events/bus.js'
import {
  type InsertOperation,
  type Operation,
  type OperationError,
  type OperationRows,
  type OperationStatus,
  OPERATION_RETENTION_SECONDS,
} from '../../../src/operations/store.js'

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

type Execution = { workflowId: string; workflowRunId: string }

/** `OperationRows` over SQL on the test database: the API's `…/operations` routes, as the agent once ran them. */
export class SqlOperations implements OperationRows {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  async #execution(id: string): Promise<Execution> {
    const [row] = await this.sql<{ workflow_id: string; workflow_run_id: string }[]>`
      SELECT workflow_id, workflow_run_id FROM ai_operations WHERE id = ${id}`
    return { workflowId: row!.workflow_id, workflowRunId: row!.workflow_run_id }
  }

  /** The key's newest operation, whatever its status: one key is one effect. */
  async find(operationKey: string): Promise<{ operation: Operation; execution: Execution } | undefined> {
    const [row] = await this.sql.unsafe<Row[]>(
      `SELECT ${COLUMNS} FROM ai_operations WHERE operation_key = $1 ORDER BY created_at DESC LIMIT 1`,
      [operationKey],
    )
    return row ? { operation: view(row), execution: await this.#execution(row.id) } : undefined
  }

  async get(id: string): Promise<{ operation: Operation; execution: Execution } | undefined> {
    const [row] = await this.sql.unsafe<Row[]>(`SELECT ${COLUMNS} FROM ai_operations WHERE id = $1`, [id])
    return row ? { operation: view(row), execution: await this.#execution(id) } : undefined
  }

  /**
   * Record an accepted operation, or return the execution's row when it has one (a
   * retried activity). Prunes operations finished more than the retention ago.
   */
  async insert(op: InsertOperation, retentionSeconds = OPERATION_RETENTION_SECONDS): Promise<Operation> {
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
    return op.operation
  }
}

