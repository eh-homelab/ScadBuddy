import { crudRead } from '../api/crudRead.js'

// The agent's questions about the backend's flow tables (#5299 D7): `workflow_runs`,
// `workflow_run_decisions` and `flow_grants`. The backend answers each over a CRUD route of
// area `flows` (backend api/crud_agent.py, flows/agent_reads.py), and the agent runs no SQL on
// `workflow_runs` or `workflow_run_decisions`. The API takes back its role's grants on them at
// every start (backend auth/revoke.py `RETIRED_GRANTS`), but the agent still connects as the
// database owner, so that binds nothing until #5299 step 12; until then the guard is
// test/retiredTables.test.ts.
//
// The gate's own read of `flow_grants` and `flow_grant_uses` (gate/store.ts `grantInput`) is not
// here: it holds the grant row's share lock across the gate's writes, so it runs on the API in
// the grant's own route (gate/crudStore.ts, backend api/crud_agent_gate.py, #5299 step 10).

export type QueueHop = { hop: string; action: string; actor: string; at: string; reason?: string | null }

/** A run's parked call (`workflow_runs.waiting_on`), as the backend's projection wrote it. */
export type WaitingOn = {
  call_id: string
  kind: 'approval' | 'answer'
  fn: string
  prompt?: string | null
  since?: string | null
  hop?: 'parent' | 'person' | null
  hop_until?: string | null
  reason?: string | null
  proposed?: boolean
  hops?: QueueHop[]
}
export type StepRow = { seq: number; fn: string; call_id: string; status: string; session_id?: string | null }

/** `started_by`'s fields; only a string is anyone's. */
export type StartedBy = { kind?: unknown; principal?: unknown; session?: unknown }

export type RunStarter = {
  /** Running or waiting, and the execution is not one a Reset replaced. */
  open: boolean
  /** The execution is neither the row's nor one a Reset replaced. */
  unrecorded: boolean
  started_by: StartedBy
}

export type DecisionChecks = { approved: boolean; approvedUnresolved: boolean; byPerson: boolean; lapsed: boolean }

export type QueueRun = {
  id: string
  name: string
  status: string
  workflow_run_id: string
  starter_session: string | null
  waiting_on: WaitingOn[]
  steps: StepRow[]
}

/** Where a page of `parentHopApprovals` goes on from: opaque. */
export type ParentHopCursor = { after_updated_at: string; after_id: string }

export type ParentHopRun = {
  id: string
  workflow_run_id: string
  session: unknown
  principal: unknown
  /** Its parked approvals at hop `parent` whose hop is live by Postgres's clock. */
  calls: WaitingOn[]
  steps: StepRow[]
}

export interface FlowRunReads {
  /** Who started run `runId`, under its row's share lock; undefined for no such run. */
  starter(runId: string, execution: string): Promise<RunStarter | undefined>
  /** What the run's decisions say of the call `requestId` approved with `inputHash`. */
  decisions(requestId: string, inputHash: string): Promise<DecisionChecks>
  /** Whether run grant `grantId` is approved, not revoked and not expired. */
  grantInForce(grantId: string): Promise<boolean>
  /** Run `runId` and its grant while it is requested; undefined for no such run (or no flows here). */
  queue(runId: string): Promise<(QueueRun & { requested_grant: { id: string; created_at: Date } | null }) | undefined>
  /** The open runs with parked calls, the 200 most recently moved first. */
  waitingRuns(): Promise<(QueueRun & { updated_at: Date })[]>
  /** Run `runId`'s steps; none for no such run. */
  steps(runId: string): Promise<StepRow[]>
  /** The waiting runs with a live parked approval at hop `parent`, oldest move first, `limit` at most. */
  parentHopApprovals(after: ParentHopCursor | null, limit: number): Promise<{ runs: ParentHopRun[]; next: ParentHopCursor | null }>
  /** Which of `requestIds` still wait at hop `parent`; undefined when this deployment has no flows. */
  stillAtParentHop(requestIds: readonly string[]): Promise<Set<string> | undefined>
}

/** The most request ids one `stillAtParentHop` read asks about (the route's bound). */
export const STILL_WAITING_BATCH = 500

type WireRun = {
  id: string
  name: string
  status: string
  workflow_run_id: string
  starter_session: string | null
  waiting_on: WaitingOn[]
  steps: StepRow[]
}

const id = encodeURIComponent

/** The backend's CRUD routes of area `flows` (see above). */
export class CrudFlowRunReads implements FlowRunReads {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  #get<T>(step: string, path: string, query?: Record<string, string>): Promise<T> {
    return crudRead<T>(this.#fetch, this.#base, step, query ? `${path}?${new URLSearchParams(query)}` : path)
  }

  async starter(runId: string, execution: string): Promise<RunStarter | undefined> {
    const { run } = await this.#get<{ run: RunStarter | null }>("a flow run's starter", `/flow-runs/${id(runId)}/starter`, { execution })
    return run ?? undefined
  }

  async decisions(requestId: string, inputHash: string): Promise<DecisionChecks> {
    const d = await this.#get<{ approved: boolean; approved_unresolved: boolean; by_person: boolean; lapsed: boolean }>(
      "a flow call's decision",
      '/flow-decisions',
      { request_id: requestId, input_hash: inputHash },
    )
    return { approved: d.approved, approvedUnresolved: d.approved_unresolved, byPerson: d.by_person, lapsed: d.lapsed }
  }

  async grantInForce(grantId: string): Promise<boolean> {
    return (await this.#get<{ in_force: boolean }>('a run grant', '/flow-grants/in-force', { grant_id: grantId })).in_force
  }

  async queue(runId: string): Promise<(QueueRun & { requested_grant: { id: string; created_at: Date } | null }) | undefined> {
    const { run } = await this.#get<{ run: (WireRun & { requested_grant: { id: string; created_at: string } | null }) | null }>(
      "a flow run's queue",
      `/flow-runs/${id(runId)}/queue`,
    )
    if (!run) return undefined
    const { requested_grant: grant, ...rest } = run
    return { ...rest, requested_grant: grant ? { id: grant.id, created_at: new Date(grant.created_at) } : null }
  }

  async waitingRuns(): Promise<(QueueRun & { updated_at: Date })[]> {
    const { runs } = await this.#get<{ runs: (WireRun & { updated_at: string })[] }>('the waiting flow runs', '/flow-runs/waiting')
    return runs.map((run) => ({ ...run, updated_at: new Date(run.updated_at) }))
  }

  async steps(runId: string): Promise<StepRow[]> {
    return (await this.#get<{ steps: StepRow[] }>("a flow run's steps", `/flow-runs/${id(runId)}/steps`)).steps
  }

  async parentHopApprovals(after: ParentHopCursor | null, limit: number): Promise<{ runs: ParentHopRun[]; next: ParentHopCursor | null }> {
    return await this.#get<{ runs: ParentHopRun[]; next: ParentHopCursor | null }>(
      'the calls waiting at hop parent',
      '/flow-runs/parent-hop-approvals',
      { limit: String(limit), ...(after ?? {}) },
    )
  }

  async stillAtParentHop(requestIds: readonly string[]): Promise<Set<string>> {
    const waiting = new Set<string>()
    for (let i = 0; i < requestIds.length; i += STILL_WAITING_BATCH) {
      const answer = await crudRead<{ waiting: string[] }>(this.#fetch, this.#base, 'the calls still at hop parent', '/flow-runs/parent-hop/waiting', {
        method: 'POST',
        body: { request_ids: requestIds.slice(i, i + STILL_WAITING_BATCH) },
      })
      for (const w of answer.waiting) waiting.add(w)
    }
    return waiting
  }
}
