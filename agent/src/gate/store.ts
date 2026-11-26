import { CrudReadError } from '../api/crudRead.js'
import type { EventLog } from '../sessions/eventLog.js'
import type { GateRoutes, Logged } from './crudStore.js'
import type { InputOutcome, Owner } from '../sessions/protocol.js'
import { type GateEntry, RespondRefusal, type Role } from './validate.js'

// Every write about a durable entry (durable-agents spec §6.6, "How a durable entry
// opens and closes"), as agent-durable's gate/store.py names them: openInput, grantInput,
// denyInput, resolveInput, run_grant_responder, flow_responder. decideInput and readInput are
// new (R5, plan Step 9.7): the respond route's write of a decision on a row that is its
// request's record, and the workflow's read of what was decided. Each function returns what
// Python's returns: false when nothing was written (already open or resolved, gone, or the
// grant does not hold).
//
// The API runs each write as one route and one transaction (#5299 step 10; gate/crudStore.ts
// `CrudGate`, tools/go/core/internal/gate and tools/go/core/internal/gate): the rows, the session
// status, the audit row, the session events and, for a resolution, `NOTIFY scadbuddy_input,
// <request id>`, so a waiter learns of it without polling. The batches of events it appended
// come back with the answer (`logged`) and are told to the event log (`committed`), which wakes
// this replica's followers and publishes `session.*` under this replica's id.

export const INPUT_CHANNEL = 'scadbuddy_input'

export type Responder = { kind: string; id: string; label: string }

/** Where the gate writes: the API's routes, and the event log its session events go through. */
export type GateDb = { events: EventLog; crud: GateRoutes }

/** Tells the event log of the batches a route's transaction appended. */
function told(db: GateDb, logged: readonly Logged[]): void {
  for (const l of logged) db.events.committed(l.session_id, l.events, l.seqs)
}

export type OpenInput = {
  requestId: string
  sessionId: string
  workflowId: string
  workflowRunId: string
  kind: 'approval' | 'answer'
  tool: string
  toolUseId: string
  summary: string
  inputHash: string | null
  prompt: string
  requestedBy: Owner
  responders: readonly ('browser' | 'grant')[]
  /** ISO 8601: when the entry's timer fires (at most 86 400 s after it parks). */
  expiresAt: string
  /** The card the panel shows (question.asked's `questions`); absent for an approval. */
  questions?: unknown[] | null
  /** An attention request's {reason, on_timeout}; absent otherwise. */
  attention?: { reason: string; on_timeout: string | null } | null
  flowRunId?: string | null
  hop?: 'parent' | 'person' | null
  /** ISO 8601. */
  hopUntil?: string | null
  parentSession?: string | null
  parentPrincipal?: string | null
  outsideReason?: string | null
  /**
   * The entry as the validator checks a response against it (R5, plan Step 9.7): with it the row
   * is the request's record, and the respond route decides it here (decideInput). Absent, the
   * workflow holds the entry, and a response goes to it as the `respond` Update.
   */
  gate?: GateRecord | null
}

/** What `ai_pending_input.gate` keeps: the validator's view of the entry, without its state (the row's). */
export type GateRecord = Omit<GateEntry, 'state'>

export type DenyInput = {
  requestId: string
  sessionId: string
  runId: string
  tool: string
  summary: string
  inputHash: string
  reason: string | null
  requestedBy: Owner
}

export type ResolveInput = {
  requestId: string
  outcome: InputOutcome
  responder: Responder
  response?: unknown
  /** The response as the person sent it, whose hash is kept for a retry's comparison (#3724). */
  rawResponse?: unknown
  reason?: string | null
}

export type GrantInput = {
  requestId: string
  sessionId: string
  tool: string
  summary: string
  /** describe_call's hash of the call: what the grant's recorded use must be for. */
  inputHash: string
  grantId: string
  requestedBy: Owner
}

export function runGrantResponder(grantId: string): Responder {
  return { kind: 'run_grant', id: grantId, label: 'Run grant' }
}

export function flowResponder(runId: string): Responder {
  return { kind: 'flow', id: runId, label: 'Flow' }
}

/**
 * Opens the entry; false when it was open already (a retried activity adds nothing).
 *
 * First, in its own transaction, the session's entries of another workflow run (a
 * Reset started a new one) are resolved `cancelled`, reason `reset`, so after a Reset
 * the read shows exactly one entry for the call.
 */
export async function openInput(db: GateDb, args: OpenInput): Promise<boolean> {
  const { opened, logged } = await db.crud.open(args)
  told(db, logged)
  return opened
}

/**
 * Records the run grant's approval of a call, only if the backend recorded the grant's
 * use of this request id, by this session, for this very hash, and the grant is in
 * force: one transaction under the grant row's share lock, so a revocation lands wholly
 * before or after it (and the agent checks the grant again as the call runs). Nothing
 * of the verdict is taken on trust. False when it does not hold; the call then parks.
 * No entry, no events: nothing is listed and nobody is notified.
 */
export async function grantInput(db: GateDb, args: GrantInput): Promise<boolean> {
  try {
    return await db.crud.grant(args)
  } catch (err) {
    // An API older than this agent (a roll that reached the agent first) has no such route: the
    // call is not granted and parks for a person, as any call the grant does not cover.
    if (err instanceof CrudReadError && !err.fromRoute && err.status === 404) return false
    throw err
  }
}

/**
 * Records the flow's refusal of a call (`on_outside="deny"`): denied, by the flow, with
 * the grant's reason; audited as a decision is. False when the request id was resolved
 * already (a retried activity writes nothing twice).
 */
export async function denyInput(db: GateDb, args: DenyInput): Promise<boolean> {
  return await db.crud.deny(args)
}

/** Resolves the entry; false when it was gone (resolved by another, or never opened). */
export async function resolveInput(db: GateDb, args: ResolveInput): Promise<boolean> {
  const { resolved, logged } = await db.crud.resolve(args)
  told(db, logged)
  return resolved
}

/**
 * A response to a request whose row is its record (decideInput), as the respond route passes it.
 * `live` says whether the row's workflow run still runs: one that ended without resolving it (a
 * terminated run, until gate/sweep.ts removes its row) is never decided, since no call would run.
 */
export type DecideInput = { requestId: string; response: unknown; responder: Responder; role: Role; live?: (workflowId: string, runId: string) => Promise<boolean> }

/** How decideInput ended: the request's outcome, or `workflow` when its workflow holds the entry (no `gate`). */
export type Decided = { kind: 'approval' | 'answer'; outcome: InputOutcome } | 'workflow'

/** How many times decideInput asks again after finding a run live: a run that changes between asks is a Reset's. */
const LIVE_CHECKS = 3

/**
 * Decides a request whose row is its record (R5, plan Step 9.7, decision Q7), as the workflow's
 * `respond` handler does for an entry it holds: the API runs the gate's validator against the
 * row's `gate` under the row's lock (state `resolving` once the row is past its timer), then
 * removes the row and writes its resolution in the same transaction. The workflow is woken only
 * after (gate/durable.ts), and a lost wake is made up by its timer's read. A missing row is
 * RespondRefusal `stale` (ended, or never asked); so is a row whose run has ended (`live`).
 *
 * With `live`, the API first answers which run the row is of, writing nothing; this asks
 * Temporal, and asks the API again naming the run it found running, which then writes only if
 * the row is still of that run. So a response the validator refuses still costs no call to
 * Temporal.
 *
 * A decision that commits while the workflow is cancelling the entry (a Stop, the turn's end:
 * the workflow's `resolving`, its row not yet gone) stands: the row decides, the cancel's
 * resolve_input then finds it gone and the workflow ends the entry as recorded.
 */
export async function decideInput(db: GateDb, args: DecideInput): Promise<Decided> {
  let liveRun: string | undefined
  for (let ask = 0; ask <= LIVE_CHECKS; ask++) {
    const answer = await db.crud.decide({
      requestId: args.requestId,
      response: args.response,
      responder: args.responder,
      role: args.role,
      checkLive: args.live !== undefined,
      ...(liveRun === undefined ? {} : { liveRun }),
    })
    if ('refused' in answer) throw new RespondRefusal(answer.refused.code, answer.refused.message)
    if ('workflow' in answer) return 'workflow'
    if ('decided' in answer) {
      told(db, answer.logged)
      return answer.decided
    }
    const { workflowId, runId } = answer.liveCheck
    if (!args.live || !(await args.live(workflowId, runId))) {
      throw new RespondRefusal('stale', `no pending input ${args.requestId}: its session's workflow run has ended`)
    }
    liveRun = runId
  }
  throw new RespondRefusal('stale', `no pending input ${args.requestId}: its session's workflow run kept changing`)
}

/** A request's resolution as its response row records it: what the workflow's `gate.read_input` returns. */
export type InputRecord = { outcome: InputOutcome; response: unknown; responder: Responder; reason: string | null }

/** The response row of `requestId`, or null while the request is open (or was never asked). */
export async function readInput(db: GateDb, args: { requestId: string }): Promise<InputRecord | null> {
  return await db.crud.read(args.requestId)
}
