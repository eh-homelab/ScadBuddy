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
 * The largest request or answer a call carries, as JSON: room past the tab's own cap on a
 * result (frontend link.ts MAX_RESULT_BYTES, 200 000) and its envelope. Sealing grows a payload
 * by about 4/3 (the codec base64s it inside JSON before it seals it, payloadCodec.ts
 * `payloadBytes`), so 256 KiB of JSON is about 350 KB sealed, under Temporal's 512 KB
 * payload-size warning and far under its 2 MB limit.
 */
export const MAX_BRIDGE_BYTES = 256 * 1024
/**
 * Past the owner's own time: the workflow's whole run, for a workflow task that is slow to start.
 * Room for one failed first workflow task too: during a rolling deploy a replica still on the
 * build before #4780 polls the unversioned `agent-tools`, has no BridgeCall, and fails the task,
 * which Temporal then gives to another replica (docs/ai/operating.md, "Rolling deploys").
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

/** The hub's side: whether a tab is held here (for the old relay), the tabs held, and running a request on one. */
export type RelayOwner = {
  holds(tabId: string): boolean
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
  /** This replica's own queue (main.ts HOST_QUEUE), which its host worker polls with `hostActivities()` (then `startHosting`). */
  hostQueue: string
  /** Where BridgeCall runs: `agent-tools`, which every replica's worker polls with the workflow bundle. */
  workflowQueue?: string
  /** Set later with `useClient` when it is made after the hub (main.ts). */
  client?: SealingClient | undefined
  ackTimeoutMs?: number
  /** `OWNER_REFRESH_MS` when omitted. */
  refreshMs?: number
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
  readonly #workflowQueue: string
  readonly #ackTimeoutMs: number
  readonly #log: (message: string) => void
  readonly #ready: (() => Promise<boolean>) | undefined
  readonly #fallback: LegacyTabRelay | undefined
  #client: SealingClient | undefined
  #owner: RelayOwner | undefined
  /** Whether this replica's host worker polls its queue with `bridge.tab` (`startHosting`): only then is a tab claimed here. */
  #hosting = false
  /** One owner-row write at a time, in order: a tab's release never lands before its claim. */
  #writes: Promise<void> = Promise.resolve()
  readonly #refreshMs: number
  #refresh: NodeJS.Timeout | undefined

  constructor(options: TemporalTabRelayOptions) {
    this.#owners = options.owners
    this.hostQueue = options.hostQueue
    this.#workflowQueue = options.workflowQueue ?? TASK_QUEUE
    this.#client = options.client
    this.#ackTimeoutMs = options.ackTimeoutMs ?? ACK_TIMEOUT_MS
    this.#log = options.log ?? ((m) => console.error(m))
    this.#ready = options.ready
    this.#fallback = options.fallback
    this.#refreshMs = options.refreshMs ?? OWNER_REFRESH_MS
  }

  useClient(client: SealingClient): void {
    this.#client = client
  }

  serve(owner: RelayOwner): void {
    this.#owner = owner
    this.#fallback?.serve(owner)
  }

  /**
   * The host worker polls `hostQueue` with `hostActivities()` (main.ts, once the worker runs):
   * from now on this replica claims its tabs in ai_tab_owners, and claims the ones it already
   * holds. Never before: a claim with nothing polling the queue would send every call there to
   * wait out the ack timeout.
   */
  startHosting(): void {
    if (this.#hosting) return
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
    let failed = false
    if (sealing) {
      try {
        const answer = await this.#viaTemporal(sealing, tabId, request, options.signal)
        if (answer === 'failed') return request.op === 'call' ? NO_ANSWER : undefined
        if (answer !== undefined) return answer
      } catch (err) {
        if (options.signal.aborted) throw abortError()
        // The owner's row could not be read, or Temporal refused the start: the call ran
        // nowhere, so the old relay may still take it. Never a throw from the tool.
        this.#failed(err)
        failed = true
      }
    }
    if (this.#fallback) return this.#fallback.forward(tabId, request, options)
    if (failed) return request.op === 'call' ? NO_ANSWER : undefined
    // Refused rather than sent to Temporal unsealed.
    if (!sealing && request.op === 'call') return { op: 'call', outcome: { ok: false, error: { code: 'no_browser', message: SEALING_NEEDED } } }
    return undefined
  }

  /**
   * The owner's answer; undefined when this path finds no replica holding the tab, so the call
   * ran nowhere; `failed` when the workflow started and then failed, so the call may have run on
   * the tab and is never sent again. Rejects when the row's read or the start failed.
   */
  async #viaTemporal(
    { client, dropKey }: SealingClient,
    tabId: string,
    request: RelayRequest,
    signal: AbortSignal,
  ): Promise<RelayAnswer | 'failed' | undefined> {
    const owner = await this.#owners.ownerOf(tabId, signal).catch((err: unknown) => {
      throw signal.aborted ? abortError() : err
    })
    // Here, the hub would have found it: a row of this queue's is a socket that closed.
    if (owner === undefined || owner.queue === this.hostQueue) return undefined
    const { queue } = owner
    const input: BridgeCallInput = { queue, tabId, request, ackTimeoutMs: this.#ackTimeoutMs }
    const workflowId = bridgeWorkflowId()
    const handle = await client.workflow.start(BRIDGE_CALL_WORKFLOW, {
      taskQueue: this.#workflowQueue,
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
          // The workflow failed or timed out (a rollout's old replica failing its tasks): the
          // owner may have run the call, so it is not sent again by the old relay.
          this.#failed(err)
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
