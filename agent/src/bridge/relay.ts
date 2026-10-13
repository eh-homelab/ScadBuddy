import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { z } from 'zod'
import { CallOutcomeSchema, type CallOutcome } from './protocol.js'

// browser_* calls between agent replicas (#1916). A tab's bridge socket is held
// by the replica it reached (bridge/hub.ts), while the call can come from any
// replica: a classic turn runs where its chat socket started it, a durable
// session's tool call wherever its activity was taken (bridge/sessionTabs.ts),
// and an MCP call on the replica holding its session (mcp/sessionRelay.ts).
// When the tab is not connected
// here, the hub forwards the call through Postgres, as the event bus carries
// `session.*` between replicas (sessions/busEvents.ts):
//
//   caller                                  owner (holds the tab's socket)
//   INSERT request row + NOTIFY call  ──▶   has the tab? DELETE … RETURNING the row
//                                     ◀──   NOTIFY ack
//                                           runs it on the tab (hub.ts, as a local call)
//   DELETE … RETURNING the answer row ◀──   INSERT answer row + NOTIFY result
//
// The channel is `scadbuddy_bridge`, not `scadbuddy_events`: the backend
// listens on that one and logs every payload it cannot decode. It is LISTENed
// on the event listener's own connection (events/pgListener.ts `listenAlso`).
//
// Bodies go in `ai_bridge_messages`, never in the NOTIFY: a payload is capped
// at 8000 bytes (https://www.postgresql.org/docs/current/sql-notify.html) and a
// tab's result may be 200 000 (frontend link.ts MAX_RESULT_BYTES). The row is
// inserted in the transaction that NOTIFYs its id, so whoever hears the NOTIFY
// can read it ("not delivered until and unless the transaction is committed",
// same page). Every replica hears a call; only one that holds the tab claims
// its row, and DELETE … RETURNING makes the claim once, so a tab id briefly on
// two replicas (a reconnect overtaking its old socket) runs the call once.
//
// No owner: nobody acks within `ackTimeoutMs`, the caller withdraws its request
// row, and the call answers "not connected", as an unknown tab does on one
// replica. An ack is what separates that from a slow tab, so the call's own
// timeout still applies to the tab. A row already gone when withdrawn was taken,
// with its ack late or lost, so the caller then waits as if acked.
// The owner applies that timeout itself, as it would to a local call; the
// caller waits a little longer before giving up on the owner.
//
// What is not covered: a NOTIFY sent while the listening connection is down
// is lost (pgListener.ts `listenAlso`), so a call or its answer can be missed
// and then times out; and a cancelled call stops waiting here but is not
// withdrawn from the tab, the same as a local one (hub.ts `call`).

export const BRIDGE_CHANNEL = 'scadbuddy_bridge'
/** How long a forwarded call waits for some replica to say it holds the tab. */
export const ACK_TIMEOUT_MS = 3_000
/** Rows older than this are nobody's any more (no owner took the request, or nobody waits for the answer). */
const STALE_AFTER = '10 minutes'
const SWEEP_EVERY_MS = 60_000

/** What a replica is asked to do with a tab it holds. */
export type RelayRequest =
  | { op: 'call'; tool: string; args: Record<string, unknown>; timeoutMs: number }
  | { op: 'status' }

/** The answer: the tab's outcome, its state, or that the tab was gone by the time it was asked. */
export type RelayAnswer =
  | { op: 'call'; outcome: CallOutcome }
  | { op: 'status'; route: string; live: string[] }
  | { op: 'gone' }

const RequestSchema = z.discriminatedUnion('op', [
  z.object({ op: z.literal('call'), tool: z.string(), args: z.record(z.string(), z.unknown()), timeoutMs: z.number() }),
  z.object({ op: z.literal('status') }),
])
/** A request row: the request, and the replica to answer. */
const CallBodySchema = z.object({ from: z.string(), request: RequestSchema })
type CallBody = { from: string; request: RelayRequest }
const AnswerSchema = z.discriminatedUnion('op', [
  z.object({ op: z.literal('call'), outcome: CallOutcomeSchema }),
  z.object({ op: z.literal('status'), route: z.string(), live: z.array(z.string()) }),
  z.object({ op: z.literal('gone') }),
])
const HeaderSchema = z.discriminatedUnion('t', [
  z.object({ t: z.literal('call'), req: z.string(), tab: z.string(), row: z.string() }),
  z.object({ t: z.literal('ack'), req: z.string(), to: z.string() }),
  z.object({ t: z.literal('result'), req: z.string(), to: z.string(), row: z.string() }),
])
type Header = z.infer<typeof HeaderSchema>

/** The hub's side: whether a tab is held here, and running a request on it. */
export type RelayOwner = {
  holds(tabId: string): boolean
  run(tabId: string, request: RelayRequest): Promise<RelayAnswer>
}

/** What the hub forwards through (TabHubOptions `relay`). */
export interface TabRelay {
  /** Becomes the owner incoming requests are run by; once, by the hub. */
  serve(owner: RelayOwner): void
  /**
   * Runs `request` on the replica holding `tabId`. Undefined when no replica
   * said it held the tab within the ack timeout. Rejects with the abort error
   * when `signal` aborts.
   */
  forward(tabId: string, request: RelayRequest, options: { signal: AbortSignal; timeoutMs: number }): Promise<RelayAnswer | undefined>
}

type Waiter = { acked: () => void; answered: (answer: RelayAnswer) => void }

export type PgTabRelayOptions = {
  /** LISTEN on BRIDGE_CHANNEL (events/pgListener.ts `listenAlso`). */
  listen: (channel: string, onNotify: (payload: string) => void) => void
  ackTimeoutMs?: number
  log?: (message: string) => void
}

export class PgTabRelay implements TabRelay {
  readonly #sql: Sql
  /** This replica's id: answers are addressed to it. */
  readonly replica = randomUUID()
  readonly #ackTimeoutMs: number
  readonly #log: (message: string) => void
  readonly #waiting = new Map<string, Waiter>()
  #owner: RelayOwner | undefined
  #lastSweep = 0

  constructor(sql: Sql, options: PgTabRelayOptions) {
    this.#sql = sql
    this.#ackTimeoutMs = options.ackTimeoutMs ?? ACK_TIMEOUT_MS
    this.#log = options.log ?? ((m) => console.error(m))
    options.listen(BRIDGE_CHANNEL, (payload) => void this.#heard(payload))
  }

  serve(owner: RelayOwner): void {
    this.#owner = owner
  }

  async forward(
    tabId: string,
    request: RelayRequest,
    { signal, timeoutMs }: { signal: AbortSignal; timeoutMs: number },
  ): Promise<RelayAnswer | undefined> {
    if (signal.aborted) throw abortError()
    const req = randomUUID()
    const row = randomUUID()
    return new Promise<RelayAnswer | undefined>((resolve, reject) => {
      let timer: NodeJS.Timeout | undefined
      const done = () => {
        clearTimeout(timer)
        signal.removeEventListener('abort', onAbort)
        this.#waiting.delete(req)
      }
      const onAbort = () => {
        done()
        reject(abortError())
      }
      const unclaimed = () => {
        // Withdrawn here, the request can no longer be taken. When it is already
        // gone an owner took it and its ack is late or was lost, so the tab may be
        // running the call: wait for it as if acked rather than say "not connected".
        this.#sql<{ id: string }[]>`DELETE FROM ai_bridge_messages WHERE id = ${row} RETURNING id`.then(
          (withdrawn) => {
            const waiter = this.#waiting.get(req)
            if (!waiter) return
            if (withdrawn.length === 0) return waiter.acked()
            done()
            resolve(undefined)
          },
          (err: unknown) => {
            done()
            this.#failed(err)
            resolve(undefined)
          },
        )
      }
      signal.addEventListener('abort', onAbort, { once: true })
      this.#waiting.set(req, {
        acked: () => {
          clearTimeout(timer)
          // The owner times the tab out itself (and answers no_answer); this is for an owner that went away.
          timer = setTimeout(() => {
            done()
            resolve({
              op: 'call',
              outcome: {
                ok: false,
                error: {
                  code: 'no_answer',
                  message:
                    `the agent replica holding the tab did not answer within ${Math.round((timeoutMs + this.#ackTimeoutMs) / 1000)} s; ` +
                    'the call may still finish, so take a snapshot before trying again',
                },
              },
            })
          }, timeoutMs + this.#ackTimeoutMs)
        },
        answered: (answer) => {
          done()
          resolve(answer)
        },
      })
      this.#send({ t: 'call', req, tab: tabId, row }, { from: this.replica, request }).then(
        // From the commit, so a slow pool does not eat the owner's time to ack.
        () => {
          if (this.#waiting.has(req) && timer === undefined) timer = setTimeout(unclaimed, this.#ackTimeoutMs)
        },
        (err: unknown) => {
          done()
          reject(err)
        },
      )
    })
  }

  /** Inserts `body` (when given) and NOTIFYs `header` in one transaction. */
  async #send(header: Header, body?: CallBody | RelayAnswer): Promise<void> {
    await this.#sweep()
    await this.#sql.begin(async (tx) => {
      if (body !== undefined && 'row' in header) {
        await tx`INSERT INTO ai_bridge_messages (id, body) VALUES (${header.row}, ${tx.json(body as never)})`
      }
      await tx`SELECT pg_notify(${BRIDGE_CHANNEL}, ${JSON.stringify(header)})`
    })
  }

  async #sweep(): Promise<void> {
    if (Date.now() - this.#lastSweep < SWEEP_EVERY_MS) return
    this.#lastSweep = Date.now()
    await this.#sql`DELETE FROM ai_bridge_messages WHERE created_at < now() - ${STALE_AFTER}::interval`.catch(
      (err: unknown) => this.#failed(err),
    )
  }

  /** The row's body, taken: it is gone for every other replica. */
  async #take(row: string): Promise<unknown> {
    const [taken] = await this.#sql<{ body: unknown }[]>`DELETE FROM ai_bridge_messages WHERE id = ${row} RETURNING body`
    return taken?.body
  }

  async #heard(payload: string): Promise<void> {
    let header: Header
    try {
      header = HeaderSchema.parse(JSON.parse(payload))
    } catch {
      return this.#log(`bridge relay: ignored a malformed ${BRIDGE_CHANNEL} payload (${payload.length} bytes)`)
    }
    try {
      switch (header.t) {
        case 'call':
          return await this.#serveCall(header)
        case 'ack':
          if (header.to === this.replica) this.#waiting.get(header.req)?.acked()
          return
        case 'result': {
          if (header.to !== this.replica) return
          // Taken even when nobody waits (it stopped): the row is ours to remove.
          const body = await this.#take(header.row)
          const answer = AnswerSchema.safeParse(body)
          if (answer.success) this.#waiting.get(header.req)?.answered(answer.data as RelayAnswer)
          return
        }
      }
    } catch (err) {
      this.#failed(err)
    }
  }

  async #serveCall(header: Extract<Header, { t: 'call' }>): Promise<void> {
    const owner = this.#owner
    if (!owner?.holds(header.tab)) return
    const taken = await this.#take(header.row)
    if (taken === undefined) return // another replica took it, or the caller gave up
    const body = CallBodySchema.safeParse(taken)
    if (!body.success) return this.#log('bridge relay: ignored a malformed request row')
    const { from, request } = body.data
    await this.#send({ t: 'ack', req: header.req, to: from })
    const answer = await owner.run(header.tab, request as RelayRequest)
    await this.#send({ t: 'result', req: header.req, to: from, row: randomUUID() }, answer)
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
