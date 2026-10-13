import type { Sql } from 'postgres'
import type { Owner } from '../sessions/protocol.js'

// A flow run's tool calls (#1057, plan 2026-10-09-durable-phase-6-flows.md Task D1):
// ProjectWorkflow (`flow-<run id>`) calls a tool on `agent-tools` with activity id
// `tool-<call id>`. The call runs as the principal that started the run (Ruling 6,
// `workflow_runs.started_by`, written by the backend from the request's author), and
// an outward one only with a person's `approved` decision for it in
// `workflow_run_decisions` (decision B), which only the backend's browser-only routes
// write. The decision is bound to the call as it was parked (#2318): its `input_hash` is
// inputHash (approvals/service.ts) of the tool and arguments the person approved, made
// by the backend (flows/approval_hash.py), and it must equal the hash of the call being
// run. Both tables are the backend's; this reads them and never writes.

// Lowercase only, as the backend's run ids (uuid4) and the codec's subjects are.
const FLOW_WORKFLOW = /^flow-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/

/** The flow run a workflow ID names, or undefined for any other workflow. */
export function flowOf(workflowId?: string): string | undefined {
  return workflowId === undefined ? undefined : FLOW_WORKFLOW.exec(workflowId)?.[1]
}

/** Who started a flow run, and whether a person approved one of its calls. */
export interface FlowRuns {
  /**
   * The run's starter as a session owner; undefined when there is no such run, when it
   * has closed, or when `execution` (the calling workflow execution's run id) is one a
   * Reset replaced (#2258): once its run is over, a new `flow-<id>` execution is lent
   * nobody's identity.
   */
  startedBy(runId: string, execution: string): Promise<Owner | undefined>
  /**
   * Whether `workflow_run_decisions` approves the call `requestId`
   * (`flow:<run>:<workflow run>:<call>`) with this `inputHash`: the call's tool and input
   * as hashed now. Without a hash nothing is approved.
   */
  approved(requestId: string, inputHash?: string): Promise<boolean>
  /**
   * Whether the call's approval for this `inputHash` came from the run's grant (#2481),
   * now revoked or expired: `approved` is false for it, and it is refused GrantRevoked.
   */
  lapsed?(requestId: string, inputHash?: string): Promise<boolean>
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

  async startedBy(runId: string, execution: string): Promise<Owner | undefined> {
    const [row] = await this.#sql<{ started_by: StartedBy }[]>`
      SELECT started_by FROM workflow_runs
      WHERE id = ${runId} AND status IN ('running', 'waiting') AND NOT (${execution} = ANY(superseded_run_ids))`
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

  async approved(requestId: string, inputHash?: string): Promise<boolean> {
    if (inputHash === undefined) return false
    const rows = await this.#sql`
      SELECT 1 FROM workflow_run_decisions d
      WHERE d.request_id = ${requestId} AND d.kind = 'approval' AND d.outcome = 'approved'
        AND d.input_hash = ${inputHash}
        -- A run grant's approval holds only while the grant is in force (#2481).
        AND (d.responder NOT LIKE 'grant:%' OR EXISTS (
          SELECT 1 FROM flow_grants g
          WHERE 'grant:' || g.id = d.responder AND g.status = 'approved'
            AND g.revoked_at IS NULL AND now() < g.expires_at))`
    return rows.length > 0
  }

  async lapsed(requestId: string, inputHash?: string): Promise<boolean> {
    if (inputHash === undefined) return false
    const rows = await this.#sql`
      SELECT 1 FROM workflow_run_decisions d
      JOIN flow_grants g ON 'grant:' || g.id = d.responder
      WHERE d.request_id = ${requestId} AND d.kind = 'approval' AND d.outcome = 'approved'
        AND d.input_hash = ${inputHash}
        AND (g.status <> 'approved' OR g.revoked_at IS NOT NULL OR g.expires_at IS NULL OR now() >= g.expires_at)`
    return rows.length > 0
  }
}
