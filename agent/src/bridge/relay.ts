import type { Client } from '@temporalio/client'
import { randomUUID } from 'node:crypto'
import { TASK_QUEUE } from '../temporal/names.js'
import {
  BRIDGE_CALL_WORKFLOW,
  BRIDGE_TAB_ACTIVITY,
  type BridgeCallAnswer,
  type BridgeCallInput,
  ownerRunMs,
  type RelayAnswer,
  type RelayRequest,
} from '../temporal/bridgeCall.js'
import type { LegacyTabRelay } from './pgRelay.js'
import type { TabOwnerStore } from './tabOwners.js'

export type { RelayAnswer, RelayRequest } from '../temporal/bridgeCall.js'

// browser_* calls between agent replicas (#4780; #1916 relayed them through Postgres). A
// tab's bridge socket is held by the replica it reached (bridge/hub.ts), while the call
// can come from any replica: a classic turn runs where its chat socket started it, a
// durable session's tool call wherever its activity was taken (bridge/sessionTabs.ts),
// and an MCP call on the replica holding its session (mcp/sessionRelay.ts). When the tab
// is not connected here, the hub forwards the call through Temporal to the replica that
// holds it:
//
//   owner (holds the tab's socket)          caller
//   hello: ai_tab_owners row ← its queue
//                                           reads the tab's row: the owner's queue
//                                           starts BridgeCall on `agent-tools`
//   activity `bridge.tab` on its queue ◀──  (temporal/bridgeCall.ts)
//   runs it on the tab (hub.ts, as a local call)
//   returns the answer                  ──▶ the workflow's result
//                                           deletes the call's payload key
//
// Each replica polls `agent-tools.<identity>` (main.ts HOST_QUEUE), so Temporal delivers
// the call to the owner only, and once (one attempt). Nothing is sent to the replicas
// that do not hold the tab.
//
// Sealed, always: the request and the answer (page snapshots, form contents, the
// editor's text) are the workflow's payloads, and every BridgeCall workflow is a subject
// of its own, `bridge-<uuid>`, sealed under a key made for it under the KEK
// (temporal/payloadCodec.ts `subjectOf`). The caller deletes that key once it has the
// answer, so the call's history cannot be read again; `sweepBridgeKeys` deletes the key
// of a call that was cancelled or whose caller went away. A bridge payload is never stored
// by reference (only a session's are): each is capped at MAX_BRIDGE_BYTES instead, inline.
// Without a sealing client (no KEK), no call goes through Temporal.
//
// No owner: there is no row, or the owner's queue has no poller and the activity is not
// taken within `ackTimeoutMs` (schedule-to-start, never retried), and the call answers
// "not connected", as an unknown tab does on one replica. The dead owner's row is then
// dropped. A tab id briefly on two replicas (a reconnect overtaking its old socket) is
// owned by the later claim: the old socket's close releases only its own queue's row.
// The owner applies the call's own timeout to the tab; the activity's start-to-close is
// that plus `ackTimeoutMs`, after which the caller gives up on the owner.
//
// For one release (#4868) the Postgres relay (bridge/pgRelay.ts) runs beside it: a replica
// still on the old build holds tabs with no row here, so a call this path cannot place
// (no owner, the owner gone or no longer holding the tab, no sealing client) is tried
// there, and this replica answers the old relay's requests for the tabs it holds.
//
// What is not covered: a cancelled call cancels its workflow, but the tab is not told,
// the same as a local one (hub.ts `call`).

/** How long a forwarded call waits for the owner's replica to take it (the old relay's ack timeout). */
export const ACK_TIMEOUT_MS = 3_000
/**
 * The largest request or answer a call carries, as JSON: past the tab's own cap on a result
 * (frontend link.ts MAX_RESULT_BYTES, 200 000), and sealed well under Temporal's 2 MB payload limit.
 */
export const MAX_BRIDGE_BYTES = 1_000_000
/** Past the owner's own time: the workflow's whole run, for a workflow task that is slow to start. */
const WORKFLOW_SLACK_MS = 10_000

/** A call's workflow ID, and the subject its payloads are sealed under. */
export const bridgeWorkflowId = (): string => `bridge-${randomUUID()}`

/** The hub's side: whether a tab is held here (for the old relay), and running a request on it. */
export type RelayOwner = {
  holds(tabId: string): boolean
  run(tabId: string, request: RelayRequest): Promise<RelayAnswer>
}

/** What the hub forwards through (TabHubOptions `relay`). */
export interface TabRelay {
  /** Becomes the owner incoming requests are run by; once, by the hub. */
  serve(owner: RelayOwner): void
  /** A tab's socket said hello here: this replica holds it now. */
  attached(tabId: string): void
  /** That socket closed, and no other socket here holds the tab. */
  detached(tabId: string): void
  /**
   * Runs `request` on the replica holding `tabId`. Undefined when no replica holds it.
   * Rejects with the abort error when `signal` aborts.
   */
  forward(tabId: string, request: RelayRequest, options: { signal: AbortSignal; timeoutMs: number }): Promise<RelayAnswer | undefined>
}

export type TemporalTabRelayOptions = {
  owners: TabOwnerStore
  /** This replica's own queue (main.ts HOST_QUEUE), which its host worker polls with `activities()`. */
  hostQueue: string
  /** Where BridgeCall runs: `agent-tools`, which every replica's worker polls with the workflow bundle. */
  workflowQueue?: string
  /** Set later with `useClient` when it is made after the hub (main.ts). */
  client?: SealingClient | undefined
  ackTimeoutMs?: number
  /** The database is migrated (db.ts `ready`): each owner-row write waits for it, and is skipped when it is down. */
  ready?: () => Promise<boolean>
  /** The old relay, for one release (#4868): tried when this path cannot place the call. */
  fallback?: LegacyTabRelay | undefined
  log?: (message: string) => void
}

/**
 * A Temporal client whose data converter seals `bridge-<uuid>` payloads (payloadCodec.ts), and
 * how a call's key is deleted once its caller has the answer.
 */
export type SealingClient = { client: Client; dropKey: (subject: string) => Promise<void> }

const SEALING_NEEDED =
  'no browser attached: the tab is connected to another agent replica, and reaching it there needs the agent ' +
  "service's secret key (SCADBUDDY_SECRET_KEY_FILE), which seals the call's contents in Temporal. Ask the operator to mount it."

export class TemporalTabRelay implements TabRelay {
  readonly #owners: TabOwnerStore
  readonly hostQueue: string
  readonly #workflowQueue: string
  readonly #ackTimeoutMs: number
  readonly #log: (message: string) => void
  readonly #ready: (() => Promise<boolean>) | undefined
  readonly #fallback: LegacyTabRelay | undefined
  #client: SealingClient | undefined
  #owner: RelayOwner | undefined
  /** One owner-row write at a time, in order: a tab's release never lands before its claim. */
  #writes: Promise<void> = Promise.resolve()

  constructor(options: TemporalTabRelayOptions) {
    this.#owners = options.owners
    this.hostQueue = options.hostQueue
    this.#workflowQueue = options.workflowQueue ?? TASK_QUEUE
    this.#client = options.client
    this.#ackTimeoutMs = options.ackTimeoutMs ?? ACK_TIMEOUT_MS
    this.#log = options.log ?? ((m) => console.error(m))
    this.#ready = options.ready
    this.#fallback = options.fallback
  }

  useClient(client: SealingClient): void {
    this.#client = client
  }

  serve(owner: RelayOwner): void {
    this.#owner = owner
    this.#fallback?.serve(owner)
  }

  /** The owner's activity, for this replica's host worker on `hostQueue`. */
  activities(): Record<string, (tabId: string, request: RelayRequest) => Promise<RelayAnswer>> {
    return {
      [BRIDGE_TAB_ACTIVITY]: async (tabId, request) => {
        if (!this.#owner) return { op: 'gone' }
        const answer = await this.#owner.run(tabId, request)
        return jsonBytes(answer) > MAX_BRIDGE_BYTES ? tooLarge('result') : answer
      },
    }
  }

  /** Drops the rows a previous process on this queue left (a restart keeps the identity). */
  releaseAll(): Promise<void> {
    return this.#write(() => this.#owners.releaseAll(this.hostQueue))
  }

  attached(tabId: string): void {
    void this.#write(() => this.#owners.claim(tabId, this.hostQueue))
  }

  detached(tabId: string): void {
    void this.#write(() => this.#owners.release(tabId, this.hostQueue))
  }

  #write(write: () => Promise<void>): Promise<void> {
    const ready = this.#ready
    this.#writes = this.#writes
      .then(async () => {
        if (ready && !(await ready())) throw new Error('the database is not ready; a tab owner row was not written')
        await write()
      })
      .catch((err: unknown) => this.#failed(err))
    return this.#writes
  }

  async forward(
    tabId: string,
    request: RelayRequest,
    options: { signal: AbortSignal; timeoutMs: number },
  ): Promise<RelayAnswer | undefined> {
    if (options.signal.aborted) throw abortError()
    if (jsonBytes(request) > MAX_BRIDGE_BYTES) return request.op === 'call' ? tooLarge('request') : undefined
    const sealing = this.#client
    if (sealing) {
      const answer = await this.#viaTemporal(sealing, tabId, request, options.signal)
      if (answer !== undefined) return answer
    }
    if (this.#fallback) return this.#fallback.forward(tabId, request, options)
    // Refused rather than sent to Temporal unsealed.
    if (!sealing && request.op === 'call') return { op: 'call', outcome: { ok: false, error: { code: 'no_browser', message: SEALING_NEEDED } } }
    return undefined
  }

  /** The owner's answer; undefined when this path finds no replica holding the tab. */
  async #viaTemporal(
    { client, dropKey }: SealingClient,
    tabId: string,
    request: RelayRequest,
    signal: AbortSignal,
  ): Promise<RelayAnswer | undefined> {
    const queue = await this.#owners.ownerOf(tabId, signal).catch((err: unknown) => {
      throw signal.aborted ? abortError() : err
    })
    // Here, the hub would have found it: a row of this queue's is a socket that closed.
    if (queue === undefined || queue === this.hostQueue) return undefined
    const input: BridgeCallInput = { queue, tabId, request, ackTimeoutMs: this.#ackTimeoutMs }
    const workflowId = bridgeWorkflowId()
    const handle = await client.workflow.start(BRIDGE_CALL_WORKFLOW, {
      taskQueue: this.#workflowQueue,
      workflowId,
      args: [input],
      workflowExecutionTimeout: this.#ackTimeoutMs + ownerRunMs(input) + WORKFLOW_SLACK_MS,
    })
    const answer = await new Promise<BridgeCallAnswer>((resolve, reject) => {
      const onAbort = () => {
        // The tab is not told, as for a local call; the workflow stops waiting for it. Its
        // key stays for the workflow's last tasks, and the sweep deletes it (sweepBridgeKeys).
        handle.cancel().catch((err: unknown) => this.#failed(err))
        reject(abortError())
      }
      if (signal.aborted) return onAbort()
      signal.addEventListener('abort', onAbort, { once: true })
      handle.result().then(
        (result: BridgeCallAnswer) => {
          signal.removeEventListener('abort', onAbort)
          resolve(result)
        },
        (err: unknown) => {
          signal.removeEventListener('abort', onAbort)
          reject(err)
        },
      )
    })
    // The workflow is closed and its answer read: nobody needs its payloads again.
    dropKey(workflowId).catch((err: unknown) => this.#failed(err))
    if (answer.op === 'unreachable') {
      // Nobody polls the owner's queue: its pod is gone, and so is its claim.
      void this.#write(() => this.#owners.release(tabId, queue))
      return undefined
    }
    return answer.op === 'gone' ? undefined : answer
  }

  #failed(err: unknown): void {
    this.#log(`bridge relay: ${err instanceof Error ? err.message : String(err)}`)
  }
}

function jsonBytes(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value), 'utf8')
}

function tooLarge(what: 'request' | 'result'): RelayAnswer {
  return {
    op: 'call',
    outcome: {
      ok: false,
      error: {
        code: 'too_large',
        message: `the call's ${what} is over the ${MAX_BRIDGE_BYTES} bytes a call to a tab on another agent replica may carry`,
      },
    },
  }
}

function abortError(): Error {
  const err = new Error('the call was cancelled')
  err.name = 'AbortError'
  return err
}
