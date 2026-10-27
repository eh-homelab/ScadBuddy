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
//
// Each replica polls `agent-tools.<identity>` (main.ts HOST_QUEUE), so Temporal delivers
// the call to the owner only, and once (one attempt). Nothing is sent to the replicas
// that do not hold the tab. The request and the answer are the workflow's payloads: a
// session's are sealed under its key, and one of OFFLOAD_BYTES or more goes to the
// payload store by reference (temporal/payloadCodec.ts), so the workflow ID names the
// session (`bridge-<session>-<call>`, `subjectOf`).
//
// No owner: there is no row, or the owner's queue has no poller and the activity is not
// taken within `ackTimeoutMs` (schedule-to-start, never retried), and the call answers
// "not connected", as an unknown tab does on one replica. The dead owner's row is then
// dropped. A tab id briefly on two replicas (a reconnect overtaking its old socket) is
// owned by the later claim: the old socket's close releases only its own queue's row.
// The owner applies the call's own timeout to the tab; the activity's start-to-close is
// that plus `ackTimeoutMs`, after which the caller gives up on the owner.
//
// What is not covered: a cancelled call cancels its workflow, but the tab is not told,
// the same as a local one (hub.ts `call`).

/** How long a forwarded call waits for the owner's replica to take it. */
export const ACK_TIMEOUT_MS = 2_000
/** Past the owner's own time: the workflow's whole run, for a workflow task that is slow to start. */
const WORKFLOW_SLACK_MS = 10_000
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

/** A call's workflow ID: under its session when it has one, so its payloads are sealed under the session's key. */
export function bridgeWorkflowId(sessionId: string | undefined): string {
  return sessionId !== undefined && UUID.test(sessionId) ? `bridge-${sessionId}-${randomUUID()}` : `bridge-${randomUUID()}`
}

/** The hub's side: running a request on a tab held here. */
export type RelayOwner = {
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
  forward(
    tabId: string,
    request: RelayRequest,
    options: { signal: AbortSignal; timeoutMs: number; sessionId?: string | undefined },
  ): Promise<RelayAnswer | undefined>
}

export type TemporalTabRelayOptions = {
  owners: TabOwnerStore
  /** This replica's own queue (main.ts HOST_QUEUE), which its host worker polls with `activities()`. */
  hostQueue: string
  /** Where BridgeCall runs: `agent-tools`, which every replica's worker polls with the workflow bundle. */
  workflowQueue?: string
  /** Set later with `useClient` when it is made after the hub (main.ts). */
  client?: Client | undefined
  ackTimeoutMs?: number
  /** The database is migrated (db.ts `ready`): each owner-row write waits for it, and is skipped when it is down. */
  ready?: () => Promise<boolean>
  log?: (message: string) => void
}

export class TemporalTabRelay implements TabRelay {
  readonly #owners: TabOwnerStore
  readonly hostQueue: string
  readonly #workflowQueue: string
  readonly #ackTimeoutMs: number
  readonly #log: (message: string) => void
  readonly #ready: (() => Promise<boolean>) | undefined
  #client: Client | undefined
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
  }

  useClient(client: Client): void {
    this.#client = client
  }

  serve(owner: RelayOwner): void {
    this.#owner = owner
  }

  /** The owner's activity, for this replica's host worker on `hostQueue`. */
  activities(): Record<string, (tabId: string, request: RelayRequest) => Promise<RelayAnswer>> {
    return {
      [BRIDGE_TAB_ACTIVITY]: async (tabId, request) => (this.#owner ? this.#owner.run(tabId, request) : { op: 'gone' }),
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
    { signal, sessionId }: { signal: AbortSignal; timeoutMs: number; sessionId?: string | undefined },
  ): Promise<RelayAnswer | undefined> {
    if (signal.aborted) throw abortError()
    const client = this.#client
    if (!client) return undefined
    const queue = await this.#owners.ownerOf(tabId, signal).catch((err: unknown) => {
      throw signal.aborted ? abortError() : err
    })
    // Here, the hub would have found it: a row of this queue's is a socket that closed.
    if (queue === undefined || queue === this.hostQueue) return undefined
    const input: BridgeCallInput = { queue, tabId, request, ackTimeoutMs: this.#ackTimeoutMs }
    const handle = await client.workflow.start(BRIDGE_CALL_WORKFLOW, {
      taskQueue: this.#workflowQueue,
      workflowId: bridgeWorkflowId(sessionId),
      args: [input],
      workflowExecutionTimeout: this.#ackTimeoutMs + ownerRunMs(input) + WORKFLOW_SLACK_MS,
    })
    const answer = await new Promise<BridgeCallAnswer>((resolve, reject) => {
      const onAbort = () => {
        // The tab is not told, as for a local call; the workflow stops waiting for it.
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
    if (answer.op !== 'unreachable') return answer
    // Nobody polls the owner's queue: its pod is gone, and so is its claim.
    void this.#write(() => this.#owners.release(tabId, queue))
    return undefined
  }

  #failed(err: unknown): void {
    this.#log(`bridge relay: ${err instanceof Error ? err.message : String(err)}`)
  }
}

function abortError(): Error {
  const err = new Error('the call was cancelled')
  err.name = 'AbortError'
  return err
}
