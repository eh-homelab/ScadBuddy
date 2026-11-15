import { wellFormed } from '../api/crudRead.js'
import { type CrudTarget, crudOf, qs } from '../api/crudStores.js'
import type { InputOutcome, Owner, ServerEvent } from '../sessions/protocol.js'
import { responseHash } from './canonical.js'
import type { DenyInput, GrantInput, InputRecord, OpenInput, ResolveInput, Responder } from './store.js'
import { REFUSALS, type RefusalCode, type Role } from './validate.js'

// The tool-call gate's writes through the API's CRUD routes (#5299 step 10, D2; backend
// api/crud_agent_gate.py, agent_data/gate.py): each of gate/store.ts's writers is one route and
// one transaction on the API, which builds the session events as store.ts does and answers the
// batches it appended (`logged`), which store.ts hands to the event log once the answer is back.
// store.ts calls these once the API lists area `gate` (`GateDb.useCrud`), and its own SQL until
// then, except the run grant's check, which reads the backend's `flow_grants` and goes only here.
//
// A request id travels in the body or the query string, never in the path. One longer than the
// API takes (`REQUEST_ID_MAX`) is never sent: no row can hold it, so a resolve, a decide, a read
// or a grant of it answers as one of an id that was never asked, as the SQL did.

/** The longest request id the routes take (backend api/crud_agent_gate.py `REQUEST_ID_MAX`). */
export const REQUEST_ID_MAX = 2048

/** A batch of a session's events the API appended in the write's transaction, with their seqs. */
export type Logged = { session_id: string; events: ServerEvent[]; seqs: number[] }

/** `decide`'s arguments: DecideInput's, with whether to ask for a live run and the one found running. */
export type DecideCall = { requestId: string; response: unknown; responder: Responder; role: Role; checkLive: boolean; liveRun?: string }

/** `decide`'s answer: exactly one of the four. */
export type DecideAnswer =
  | { refused: { code: RefusalCode; message: string } }
  | { workflow: true }
  | { liveCheck: { workflowId: string; runId: string } }
  | { decided: { kind: 'approval' | 'answer'; outcome: InputOutcome }; logged: Logged[] }

/** The gate's routes, as store.ts calls them. */
export interface GateRoutes {
  open(args: OpenInput): Promise<{ opened: boolean; logged: Logged[] }>
  grant(args: GrantInput): Promise<boolean>
  deny(args: DenyInput): Promise<boolean>
  resolve(args: ResolveInput): Promise<{ resolved: boolean; logged: Logged[] }>
  decide(args: DecideCall): Promise<DecideAnswer>
  read(requestId: string): Promise<InputRecord | null>
}

const principal = (who: Owner | Responder) => ({ kind: who.kind, id: who.id, label: who.label })
const fits = (requestId: string): boolean => requestId.length > 0 && requestId.length <= REQUEST_ID_MAX

type WireDecide = {
  refused: { code: string; message: string } | null
  workflow: boolean
  live_check: { workflow_id: string; run_id: string } | null
  decided: { kind: 'approval' | 'answer'; outcome: InputOutcome } | null
  logged: Logged[]
}

/** GateRoutes over the API's CRUD routes, area `gate`. */
export class CrudGate implements GateRoutes {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async open(args: OpenInput): Promise<{ opened: boolean; logged: Logged[] }> {
    const body = {
      request_id: args.requestId,
      session_id: args.sessionId,
      workflow_id: args.workflowId,
      workflow_run_id: args.workflowRunId,
      kind: args.kind,
      tool: args.tool,
      tool_use_id: args.toolUseId,
      summary: args.summary,
      input_hash: args.inputHash,
      prompt: args.prompt,
      requested_by: principal(args.requestedBy),
      responders: [...args.responders],
      expires_at: args.expiresAt,
      questions: args.questions ?? null,
      attention: args.attention ?? null,
      flow_run_id: args.flowRunId ?? null,
      hop: args.hop ?? null,
      hop_until: args.hopUntil ?? null,
      parent_session: args.parentSession ?? null,
      parent_principal: args.parentPrincipal ?? null,
      outside_reason: args.outsideReason ?? null,
      gate: args.gate ?? null,
    }
    return await this.#call('an entry opened', '/gate/open', { body: wellFormed(body) })
  }

  async grant(args: GrantInput): Promise<boolean> {
    if (!fits(args.requestId)) return false
    const body = {
      request_id: args.requestId,
      session_id: args.sessionId,
      tool: args.tool,
      summary: args.summary,
      input_hash: args.inputHash,
      grant_id: args.grantId,
      requested_by: principal(args.requestedBy),
    }
    const { granted } = await this.#call<{ granted: boolean }>("a run grant's approval", '/gate/grant', { body: wellFormed(body) })
    return granted
  }

  async deny(args: DenyInput): Promise<boolean> {
    const body = {
      request_id: args.requestId,
      session_id: args.sessionId,
      run_id: args.runId,
      tool: args.tool,
      summary: args.summary,
      input_hash: args.inputHash,
      reason: args.reason,
      requested_by: principal(args.requestedBy),
    }
    const { denied } = await this.#call<{ denied: boolean }>("a flow's refusal", '/gate/deny', { body: wellFormed(body) })
    return denied
  }

  async resolve(args: ResolveInput): Promise<{ resolved: boolean; logged: Logged[] }> {
    if (!fits(args.requestId)) return { resolved: false, logged: [] }
    const body = {
      request_id: args.requestId,
      outcome: args.outcome,
      responder: principal(args.responder),
      response: args.response ?? null,
      response_hash: args.rawResponse === undefined ? null : responseHash(args.rawResponse),
      reason: args.reason ?? null,
    }
    return await this.#call('an entry resolved', '/gate/resolve', { body: wellFormed(body) })
  }

  async decide(args: DecideCall): Promise<DecideAnswer> {
    if (!fits(args.requestId)) return { refused: { code: 'stale', message: `no pending input ${args.requestId}: it is stale or was never asked` } }
    const body = {
      request_id: args.requestId,
      response: args.response,
      responder: principal(args.responder),
      role: args.role,
      check_live: args.checkLive,
      live_run: args.liveRun ?? null,
    }
    const a = await this.#call<WireDecide>('a response decided', '/gate/decide', { body: wellFormed(body) })
    if (a.refused) {
      const code = a.refused.code
      if (!(REFUSALS as readonly string[]).includes(code)) throw new Error(`the API refused a response with an unknown code ${JSON.stringify(code)}`)
      return { refused: { code: code as RefusalCode, message: a.refused.message } }
    }
    if (a.workflow) return { workflow: true }
    if (a.live_check) return { liveCheck: { workflowId: a.live_check.workflow_id, runId: a.live_check.run_id } }
    if (a.decided) return { decided: a.decided, logged: a.logged }
    throw new Error('the API answered a decide with no outcome')
  }

  async read(requestId: string): Promise<InputRecord | null> {
    if (!fits(requestId)) return null
    const { record } = await this.#call<{ record: InputRecord | null }>("a request's response", `/gate/response${qs({ request_id: requestId })}`)
    return record
  }
}
