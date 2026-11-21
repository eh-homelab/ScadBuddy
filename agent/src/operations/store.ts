import { createHash } from 'node:crypto'
import { crudRead, wellFormed } from '../api/crudRead.js'
import type { OperationActor } from '../temporal/names.js'

// `ai_operations` (spec 2026-10-01 §4.2 "Our record", #1055): one row per execution of
// an AgentOperation workflow, written only by its activities, as the backend's
// `operations` (backend/scadbuddy/operations/store.py) is by its `Operation`'s.
//
// Since #5299 step 8 the API owns the table: every method goes through `CrudOperations`, the
// backend's api/crud_agent_tasks.py routes, which write the operation's activity row in the same
// transaction.

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

/** What `OperationStore` reads and writes through: the API's CRUD routes (`CrudOperations`). */
export type OperationRows = Pick<CrudOperations, 'find' | 'get' | 'insert' | 'finish'>

export class OperationStore {
  private readonly rows: OperationRows

  constructor(rows: OperationRows) {
    this.rows = rows
  }

  /** The key's newest operation, whatever its status: one key is one effect. */
  async find(operationKey: string): Promise<Operation | undefined> {
    return (await this.rows.find(operationKey))?.operation
  }

  async get(id: string): Promise<Operation | undefined> {
    return (await this.rows.get(id))?.operation
  }

  /** The execution that runs (or ran) an operation. */
  async execution(id: string): Promise<{ workflowId: string; workflowRunId: string } | undefined> {
    return (await this.rows.get(id))?.execution
  }

  /**
   * Record an accepted operation, or return the execution's row when it has one (a
   * retried activity). Prunes operations finished more than the retention ago.
   */
  insert(op: InsertOperation, retentionSeconds = OPERATION_RETENTION_SECONDS): Promise<Operation> {
    return this.rows.insert(op, retentionSeconds)
  }

  /** End a running operation; a retried end finds it ended and changes nothing. */
  finish(id: string, outcome: { result: unknown } | { error: OperationError }): Promise<Operation> {
    return this.rows.finish(id, outcome)
  }
}

/** A row of `ai_operations` as the routes answer it (backend api/crud_agent_tasks.py `Operation`). */
type WireOperation = Omit<Operation, 'repeated'> & { workflow_id: string; workflow_run_id: string }

type Execution = { workflowId: string; workflowRunId: string }

/** The operation, with its times as ISO strings, and the execution that runs it. */
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
