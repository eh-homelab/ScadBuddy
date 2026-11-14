import type { Sql } from 'postgres'
import { type FlowRunReads, SqlFlowRunReads } from '../flows/backendReads.js'
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
// run. A person's approval of a print_output call that names a registry printer binds to
// the arguments plus the printer_id the row links (flows/print_binding.py, #4445). Both
// tables are the backend's; the agent asks it over its CRUD routes (flows/backendReads.ts,
// #5299 D7) and never writes them.

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
  /**
   * Whether `approved` holds for an approval that never saw what the call resolves to
   * (#4445): a run grant's match, or a narrowing (`response.narrowed_by`). A person's own
   * decision on the parked call binds the resolution, so it never counts here.
   */
  approvedUnresolved?(requestId: string, inputHash?: string): Promise<boolean>
  /** Whether a person approved the call (responder `browser`), not the run's grant (#959). */
  byPerson?(requestId: string, inputHash?: string): Promise<boolean>
}

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
  if (id === 'browser' || id.startsWith('user:')) return { kind: 'browser', id, label: 'You' }
  if (id.startsWith('token:')) return { kind: 'bearer', id, label: `MCP ${id}` }
  if (id.startsWith('oidc:')) return { kind: 'oidc', id, label: `MCP OIDC ${id}` }
  if (id.startsWith('anonymous:')) return { kind: 'anonymous', id, label: `MCP ${id}` }
  return undefined
}

export class PgFlowRuns implements FlowRuns {
  readonly #sql: Sql
  readonly #reads: FlowRunReads

  /** `reads`: the backend's flow tables (flows/backendReads.ts); its SQL on `sql` by default. */
  constructor(sql: Sql, reads?: FlowRunReads) {
    this.#sql = sql
    this.#reads = reads ?? new SqlFlowRunReads(sql)
  }

  async startedBy(runId: string, execution: string): Promise<Owner | undefined> {
    // A Reset of a closed run (backend flows/store.py _reset_run) holds the row FOR
    // UPDATE while it starts the new execution, and opens it only after: the lock waits
    // for that, or the new execution's first call reads the old closed row and is
    // refused. It is taken by id alone, as a row a WHERE filters out is never waited on.
    // The backend's route takes it in its own transaction (#5299 D7); the SQL fallback for an
    // older API pod takes it on the agent's role, where FOR SHARE needs UPDATE on
    // workflow_runs, not only SELECT. Every call also waits behind a writer's FOR UPDATE on
    // the row (_project, _reset_run), which is held only briefly.
    const row = await this.#reads.starter(runId, execution)
    if (row && !row.open && row.unrecorded) {
      // Closed, and this execution is neither the row's nor one a Reset replaced: a Reset
      // that started it and has not committed yet (_reset_run's "landed although this
      // call failed" path, which its own retry commits). Thrown as a lookup failure, so
      // the activity's retries wait for that commit rather than refusing for good. That
      // window is short: a flow tool call retries at most 3 times
      // (backend workflows/flow_tools.py, maximum_attempts=3), seconds with the default
      // backoff, far less than a Reset operation's own retry. A Reset that takes longer
      // to commit fails the call, and the flow's own step reports it.
      throw new Error(`flow run ${runId} is closed and has not recorded execution ${execution} yet`)
    }
    if (!row?.open) return undefined
    const by = row.started_by
    // The signed-in person who started it, when one did (#4975 phase 5).
    if (by.kind === 'browser') {
      return { kind: 'browser', id: typeof by.principal === 'string' ? decoded(by.principal) : 'browser', label: 'You' }
    }
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
    return (await this.#reads.decisions(requestId, inputHash)).approved
  }

  async approvedUnresolved(requestId: string, inputHash?: string): Promise<boolean> {
    if (inputHash === undefined) return false
    return (await this.#reads.decisions(requestId, inputHash)).approvedUnresolved
  }

  async byPerson(requestId: string, inputHash?: string): Promise<boolean> {
    if (inputHash === undefined) return false
    return (await this.#reads.decisions(requestId, inputHash)).byPerson
  }

  async lapsed(requestId: string, inputHash?: string): Promise<boolean> {
    if (inputHash === undefined) return false
    return (await this.#reads.decisions(requestId, inputHash)).lapsed
  }
}
