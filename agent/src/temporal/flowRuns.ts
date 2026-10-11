import type { Sql } from 'postgres'
import type { Owner } from '../sessions/protocol.js'

// A flow run's tool calls (#1057, plan 2026-10-09-durable-phase-6-flows.md Task D1):
// ProjectWorkflow (`flow-<run id>`) calls a tool on `agent-tools` with activity id
// `tool-<call id>`. The call runs as the principal that started the run (Ruling 6,
// `workflow_runs.started_by`, written by the backend from the request's author), and
// an outward one only with a person's `approved` decision for it in
// `workflow_run_decisions` (decision B), which only the backend's browser-only routes
// write. Both tables are the backend's; this reads them and never writes.

// Lowercase only, as the backend's run ids (uuid4) and the codec's subjects are.
const FLOW_WORKFLOW = /^flow-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/

/** The flow run a workflow ID names, or undefined for any other workflow. */
export function flowOf(workflowId?: string): string | undefined {
  return workflowId === undefined ? undefined : FLOW_WORKFLOW.exec(workflowId)?.[1]
}

/** Who started a flow run, and whether a person approved one of its calls. */
export interface FlowRuns {
  /** The run's starter as a session owner; undefined when there is no such run. */
  startedBy(runId: string): Promise<Owner | undefined>
  /** Whether `workflow_run_decisions` approves the call `requestId` (`flow:<run>:<workflow run>:<call>`). */
  approved(requestId: string): Promise<boolean>
}

/** `started_by` as the backend writes it (core/authorship.py `current_author()`). */
type StartedBy = { kind?: unknown; principal?: unknown; session?: unknown }

function decoded(header: string): string {
  // The backend keeps the author header as sent: tools/authorship.ts percent-encodes it.
  try {
    return decodeURIComponent(header)
  } catch {
    return header
  }
}

/** An author's principal id as an owner, mapped as approvals/mcp.ts maps a principal. */
function ownerOfPrincipal(id: string): Owner | undefined {
  if (id === 'browser') return { kind: 'browser', id, label: 'You' }
  if (id.startsWith('token:')) return { kind: 'bearer', id, label: `MCP ${id}` }
  if (id.startsWith('oidc:')) return { kind: 'oidc', id, label: `MCP OIDC ${id}` }
  if (id.startsWith('anonymous:')) return { kind: 'anonymous', id, label: `MCP ${id}` }
  return undefined
}

export class PgFlowRuns implements FlowRuns {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async startedBy(runId: string): Promise<Owner | undefined> {
    const [row] = await this.#sql<{ started_by: StartedBy }[]>`SELECT started_by FROM workflow_runs WHERE id = ${runId}`
    if (!row) return undefined
    const by = row.started_by
    if (by.kind === 'browser') return { kind: 'browser', id: 'browser', label: 'You' }
    if (by.kind !== 'agent' || typeof by.principal !== 'string') return undefined
    // Started from a session: its owner, as that session's own turns run.
    if (typeof by.session === 'string') {
      const [session] = await this.#sql<{ owner_kind: Owner['kind']; owner_id: string; owner_label: string }[]>`
        SELECT owner_kind, owner_id, owner_label FROM ai_sessions WHERE id::text = ${by.session}`
      if (session) return { kind: session.owner_kind, id: session.owner_id, label: session.owner_label }
    }
    return ownerOfPrincipal(decoded(by.principal))
  }

  async approved(requestId: string): Promise<boolean> {
    const rows = await this.#sql`
      SELECT 1 FROM workflow_run_decisions
      WHERE request_id = ${requestId} AND kind = 'approval' AND outcome = 'approved'`
    return rows.length > 0
  }
}
