import { type Client, TimeoutFailure, WorkflowFailedError } from '@temporalio/client'
import { randomUUID } from 'node:crypto'
import {
  BRIDGE_CALL_WORKFLOW,
  BRIDGE_TAB_ACTIVITY,
  type BridgeCallAnswer,
  type BridgeCallInput,
  ownerRunMs,
  type RelayAnswer,
  type RelayRequest,
} from '../temporal/bridgeCall.js'
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
//                                           starts BridgeCall on its own queue
//   activity `bridge.tab` on its queue ◀──  (temporal/bridgeCall.ts)
//   runs it on the tab (hub.ts, as a local call)
//   returns the answer                  ──▶ the workflow's result
//                                           deletes the call's payload key
//
// Each replica polls `agent-tools.<identity>` (main.ts HOST_QUEUE), so Temporal delivers
// the call to the owner only, and once (one attempt). Nothing is sent to the replicas
// that do not hold the tab. The workflow's own tasks run on the CALLER's queue, which its
// host worker polls with BridgeCall (#5862): both queues are unversioned and polled by
// one replica each, so no hop waits on the shared `agent-tools` queue. That queue is
// versioned, and in prod a BridgeCall's first workflow task there waited 3.4 s for a
// poller, past the status probe's budget (hub.ts STATUS_PROBE_MS).
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
// Without a sealing client the call is refused (SEALING_NEEDED), never sent another way: the
// Postgres relay that ran beside this path for one release is gone (#4868).
//
// What is not covered: a cancelled call cancels its workflow, but the tab is not told,
// the same as a local one (hub.ts `call`).

/** How long a forwarded call waits for the owner's replica to take it (its schedule-to-start timeout). */
export const ACK_TIMEOUT_MS = 3_000
/**
 * The largest request or answer a call carries, as JSON: room past the tab's own cap on a
 * result (frontend link.ts MAX_RESULT_BYTES, 200 000) and its envelope. Sealing grows a payload
 * by about 4/3 (the codec base64s it inside JSON before it seals it, payloadCodec.ts
 * `payloadBytes`), so 256 KiB of JSON is about 350 KB sealed, under Temporal's 512 KB
 * payload-size warning and far under its 2 MB limit.
 */
export const MAX_BRIDGE_BYTES = 256 * 1024
/**
 * Past the owner's own time: the workflow's whole run, for its workflow tasks on the caller's own
 * host worker, which are CPU work there (start, unsealing, the result: up to 4.3 s on a loaded
 * replica, #5428), and for that worker's reconnect if Temporal blips mid-call. Past it the call
 * answers no_answer.
 */
const WORKFLOW_SLACK_MS = 15_000

/**
 * How often a replica refreshes its tabs' rows (`TabOwnerStore.refresh`): a claim lost while
 * the database was down, or a row a call that missed this replica's queue dropped, is back
 * within this. A row naming another replica is never taken back.
 */
export const OWNER_REFRESH_MS = 30_000

/** A call's workflow ID, and the subject its payloads are sealed under. */
export const bridgeWorkflowId = (): string => `bridge-${randomUUID()}`

/** The hub's side: the tabs held here, and running a request on one. */
export type RelayOwner = {
  /** Every tab held here now, for the owner rows' refresh. */
  tabs(): string[]
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
  /** Stops anything the relay runs on its own (the hub's close). */
  close?(): void
}

export type TemporalTabRelayOptions = {
  owners: TabOwnerStore
  /**
   * This replica's own queue (main.ts HOST_QUEUE), which its host worker polls with
   * `hostActivities()` (then `startHosting`) and with the BridgeCall workflow: the calls this
   * replica starts run their workflow tasks there (#5862).
   */
  hostQueue: string
  /** Set later with `useClient` when it is made after the hub (main.ts). */
  client?: SealingClient | undefined
  ackTimeoutMs?: number
  /** `OWNER_REFRESH_MS` when omitted. */
  refreshMs?: number
  /** The database is migrated (db.ts `ready`): each owner-row write waits for it, and is skipped when it is down. */
  ready?: () => Promise<boolean>
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

const NO_SEALING: RelayAnswer = { op: 'call', outcome: { ok: false, error: { code: 'no_browser', message: SEALING_NEEDED } } }

const NO_ANSWER: RelayAnswer = {
  op: 'call',
  outcome: {
    ok: false,
    error: {
      code: 'no_answer',
      message:
        'the call to the agent replica holding the tab failed on its way (see the agent log); ' +
        'it may still have run, so take a snapshot before trying again',
    },
  },
}

export class TemporalTabRelay implements TabRelay {
  readonly #owners: TabOwnerStore
  readonly hostQueue: string
  readonly #ackTimeoutMs: number
  readonly #log: (message: string) => void
  readonly #ready: (() => Promise<boolean>) | undefined
  #client: SealingClient | undefined
  #owner: RelayOwner | undefined
  /** Whether this replica's host worker polls its queue with `bridge.tab` (`startHosting`): only then is a tab claimed here. */
  #hosting = false
  /** `startHosting` refused for want of a sealing client, and said so once. */
  #refused = false
  /** One owner-row write at a time, in order: a tab's release never lands before its claim. */
  #writes: Promise<void> = Promise.resolve()
  readonly #refreshMs: number
  #refresh: NodeJS.Timeout | undefined

  constructor(options: TemporalTabRelayOptions) {
    this.#owners = options.owners
    this.hostQueue = options.hostQueue
    this.#client = options.client
    this.#ackTimeoutMs = options.ackTimeoutMs ?? ACK_TIMEOUT_MS
    this.#log = options.log ?? ((m) => console.error(m))
    this.#ready = options.ready
    this.#refreshMs = options.refreshMs ?? OWNER_REFRESH_MS
  }

  useClient(client: SealingClient): void {
    this.#client = client
  }

  serve(owner: RelayOwner): void {
    this.#owner = owner
  }

  /**
   * The host worker polls `hostQueue` with `hostActivities()` (main.ts, once the worker runs):
   * from now on this replica claims its tabs in ai_tab_owners, and claims the ones it already
   * holds. Never before: a claim with nothing polling the queue would send every call there to
   * wait out the ack timeout. Never without a sealing client (`useClient`) either: a replica
   * with no KEK cannot decode the sealed calls other replicas send it, so each would answer
   * no_answer (#4949). Its tabs then reach only the calls made on this replica.
   */
  startHosting(): void {
    if (this.#hosting) return
    if (!this.#client) {
      if (!this.#refused) {
        this.#refused = true
        this.#log('bridge relay: no sealing client (no secret key), so no tab is claimed here; other replicas cannot reach this one\'s tabs')
      }
      return
    }
    this.#hosting = true
    // The tabs are read when the write runs, after any release queued before it.
    const refresh = () => void this.#write(() => this.#owners.refresh(this.#owner?.tabs() ?? [], this.hostQueue))
    refresh()
    this.#refresh = setInterval(refresh, this.#refreshMs)
    this.#refresh.unref()
  }

  /** The owner's activity, for this replica's host worker on `hostQueue`; claims start with `startHosting`. */
  hostActivities(): Record<string, (tabId: string, request: RelayRequest) => Promise<RelayAnswer>> {
    return {
      [BRIDGE_TAB_ACTIVITY]: async (tabId, request) => {
        if (!this.#owner) return { op: 'gone' }
        const answer = await this.#owner.run(tabId, request)
        return jsonBytes(answer) > MAX_BRIDGE_BYTES ? tooLarge('result') : answer
      },
    }
  }

  close(): void {
    clearInterval(this.#refresh)
    this.#refresh = undefined
  }

  /** Drops the rows a previous process on this queue left (a restart keeps the identity). */
  releaseAll(): Promise<void> {
    return this.#write(() => this.#owners.releaseAll(this.hostQueue))
  }

  attached(tabId: string): void {
    if (this.#hosting) void this.#write(() => this.#owners.claim(tabId, this.hostQueue))
  }

  detached(tabId: string): void {
    if (this.#hosting) void this.#write(() => this.#owners.release(tabId, this.hostQueue))
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
    try {
      const answer = await this.#viaTemporal(sealing, tabId, request, options.signal)
      if (answer === 'failed') return request.op === 'call' ? NO_ANSWER : undefined
      return answer
    } catch (err) {
      if (options.signal.aborted) throw abortError()
      // The owner's row could not be read, or Temporal refused the start: the call ran
      // nowhere. Never a throw from the tool.
      this.#failed(err)
      return request.op === 'call' ? NO_ANSWER : undefined
    }
  }

  /**
   * The owner's answer; undefined when this path finds no replica holding the tab, so the call
   * ran nowhere; `failed` when the workflow started and then failed, so the call may have run on
   * the tab and is never sent again. Rejects when the row's read or the start failed.
   */
  async #viaTemporal(
    sealing: SealingClient | undefined,
    tabId: string,
    request: RelayRequest,
    signal: AbortSignal,
  ): Promise<RelayAnswer | 'failed' | undefined> {
    const owner = await this.#owners.ownerOf(tabId, signal).catch((err: unknown) => {
      throw signal.aborted ? abortError() : err
    })
    // Here, the hub would have found it: a row of this queue's is a socket that closed.
    if (owner === undefined || owner.queue === this.hostQueue) return undefined
    // A tab on another replica, but nothing sealed to reach it: refused rather than sent to
    // Temporal unsealed; a status read answers "not connected".
    if (!sealing) return request.op === 'call' ? NO_SEALING : undefined
    const { client, dropKey } = sealing
    const { queue } = owner
    const input: BridgeCallInput = { queue, tabId, request, ackTimeoutMs: this.#ackTimeoutMs }
    const workflowId = bridgeWorkflowId()
    const handle = await client.workflow.start(BRIDGE_CALL_WORKFLOW, {
      // This replica's own queue: its own host worker runs the workflow tasks, at once (#5862).
      taskQueue: this.hostQueue,
      workflowId,
      args: [input],
      workflowExecutionTimeout: this.#ackTimeoutMs + ownerRunMs(input) + WORKFLOW_SLACK_MS,
    })
    const answer = await new Promise<BridgeCallAnswer | 'failed'>((resolve, reject) => {
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
          // The workflow failed or timed out: the owner may have run the call, so it is never
          // sent again.
          if (executionTimedOut(err)) {
            // Named apart from an owner's failure (#4950): the workflow's own tasks, on this
            // replica's host worker, did not finish within WORKFLOW_SLACK_MS (a worker that
            // stopped polling, or failing tasks).
            this.#log(
              `bridge relay: the call to tab ${tabId} on ${queue} timed out as a workflow (is this replica's host worker on ${this.hostQueue} polling? its workflow tasks may have failed): ${errorMessage(err)}`,
            )
          } else {
            this.#failed(err)
          }
          resolve('failed')
        },
      )
    })
    // The workflow is closed and its answer read: nobody needs its payloads again.
    dropKey(workflowId).catch((err: unknown) => this.#failed(err))
    if (answer === 'failed') return answer
    if (answer.op === 'unreachable') {
      // Nobody polled the owner's queue in time: its pod is gone, and so is its claim. Only the
      // row as read: a claim or refresh since then is a live owner's (OWNER_REFRESH_MS).
      void this.#write(() => this.#owners.release(tabId, queue, owner.claimedAt))
      return undefined
    }
    return answer.op === 'gone' ? undefined : answer
  }

  #failed(err: unknown): void {
    this.#log(`bridge relay: ${errorMessage(err)}`)
  }
}

/** How often `hostTabs` asks whether the host worker polls yet. */
export const HOSTING_POLL_MS = 200

/**
 * How main.ts starts this replica's tab hosting (#5421): the sealing client first, when there is
 * one, then `startHosting` once `worker` (the host worker on `relay.hostQueue`) polls, asked now
 * and every `pollMs`. In that order, or `startHosting` would refuse as for a replica with no KEK.
 * Without `worker` the client is set and no tab is claimed. Returns a stop for the wait.
 */
export function hostTabs(
  relay: TemporalTabRelay,
  options: { sealing: SealingClient | undefined; worker: { state(): string } | undefined; pollMs?: number },
): () => void {
  const { sealing, worker } = options
  if (sealing) relay.useClient(sealing)
  if (!worker) return () => {}
  const polled = () => {
    if (worker.state() !== 'ok') return false
    relay.startHosting()
    return true
  }
  if (polled()) return () => {}
  // Unref'd, so a worker that never connects does not hold the process open at shutdown.
  const polling = setInterval(() => {
    if (polled()) clearInterval(polling)
  }, options.pollMs ?? HOSTING_POLL_MS)
  polling.unref()
  return () => clearInterval(polling)
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** Whether a workflow's result failed on its execution timeout (the client's WorkflowFailedError of a TimeoutFailure). */
function executionTimedOut(err: unknown): boolean {
  return err instanceof WorkflowFailedError && err.cause instanceof TimeoutFailure
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
