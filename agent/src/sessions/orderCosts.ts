import type { Sql } from 'postgres'
import type { LockClient, LockLease } from '../db.js'
import { RECORD_WAIT_MS, waitAtMost } from '../db/bounded.js'
import type { TouchedCall, TouchedSink } from './touched.js'

// The assistant's spend on an order (#5330, job cost spec 2026-11-05 §4.4). A session works
// on an order item when it gives it a design revision (order_item_revision_add, or
// _after_acceptance): the request-to-model flow ends with one (storefront spec §4.7). Each
// such call that succeeded is one ai_session_order_costs row (`record`, a TouchedSink beside
// SessionResources). When the turn ends (temporal/session/finishActivity.ts) `report` sends
// the session's spend since its last report to the backend's CRUD route
// `POST /api/v1/internal/crud/agent/orders/{id}/assistant-cost` with the agent's service
// credential; the backend writes it as one `assistant` cost entry, source
// `ai_session:<id>`, in the order's currency. The backend never reads ai_sessions (#5295).
//
// The spend since the last report is split equally between the orders still to report; an
// order with one revised item gets the entry on that item, one with several on the order.
// The backend writes one entry per order and session, so an order already reported in this
// session records nothing more. A report the route refuses for good (404 the order gone,
// since an item gone falls back to the order; 409 a currency no rate reaches, or a visitor's
// cart not yet submitted, `order_not_submitted`; 400 or 422 a path or body it cannot take) is
// dropped, and its share is counted as carried: no other order's entry takes it, so it is in
// no entry. Any other failure (no credential yet or a dead one, 401 and 403; 429; a 5xx; the
// backend out of reach) keeps it, with the share it sent (`attempted_usd`), and the next
// report sends that same share again: an answer lost after the backend wrote the entry loses
// no spend. A share the backend found under one minor unit of the order's currency (it
// answers no entry) stays to report, with the next turn's spend; once the session is over
// (`done`, `failed`, or `idle` with no session activity for IDLE_OVER_AFTER), such a share is
// closed out, reported with nothing carried. A session that works again later loses nothing:
// the closed-out spend was never carried, so it folds into the next turn's share. One turn's reports
// share one deadline (REPORT_DEADLINE_MS); what it leaves waits for the next turn's end. The
// finish activity does not wait for `report`. A session runs one report at a time across
// replicas (a session-level advisory lock on a connection of the lock client, never one of
// the stores', freed with it if the process dies). `record` waits at most RECORD_WAIT_MS for
// its row, so a revision call never waits on it. `sweep` (at start and hourly, main.ts)
// reports what a session's last turn left, for a session no turn is running, SWEEP_LIMIT
// sessions at most, the ones tried longest ago (`tried_at`) first.
// Neither `record` nor `report` ever throws: a cost report never fails a call or a turn.

export const REVISION_TOOLS: ReadonlySet<string> = new Set(['order_item_revision_add', 'order_item_revision_add_after_acceptance'])

/** One order's share of a session's spend, as the CRUD route takes it. */
export type AssistantCostReport = {
  orderId: string
  orderItemId: string | null
  sessionId: string
  /** The session's owner, for the entry's `created_by`. */
  principal: string | null
  /** USD as a decimal string. */
  costUsd: string
}

/**
 * `recorded` (written now or before), `refused` (400, 404, 409 or 422 from the route: never sent
 * again), `deferred` (under one minor unit: nothing written, sent again with more spend),
 * `failed` (anything else: the same share sent again at the next report).
 */
export type ReportAnswer = 'recorded' | 'refused' | 'deferred' | 'failed'
export type PostCost = (report: AssistantCostReport, timeoutMs: number) => Promise<ReportAnswer>

export const ASSISTANT_COST_PATH = (orderId: string) => `/api/v1/internal/crud/agent/orders/${encodeURIComponent(orderId)}/assistant-cost`
/** How long one order's report waits for the backend. */
export const REPORT_TIMEOUT_MS = 5_000
/** How long all of one turn's reports may take. */
export const REPORT_DEADLINE_MS = 15_000
/** The route's refusals that no later try mends: the order gone (an item gone falls back to the order), its currency or an unsubmitted cart, the body or path. */
const FINAL_REFUSALS: ReadonlySet<number> = new Set([400, 404, 409, 422])
/** The advisory lock a session's report holds, by `hashtextextended` of this plus the session id. */
const LOCK_PREFIX = 'ai_session_order_costs:'
/** How long a row waits before the sweep reports it: past any turn that could still report it. */
export const SWEEP_AFTER = '15 minutes'
/**
 * How long an idle session has had no activity (`ai_sessions.updated_at`, which a turn's start
 * and end move, as a handoff and other writes of the row do) before its sub-unit share is
 * closed out.
 */
export const IDLE_OVER_AFTER = '1 hour'
/** The most sessions one sweep reports: each may take up to REPORT_DEADLINE_MS. */
export const SWEEP_LIMIT = 20
/** Less time left than this, and the rest wait for the next turn's end. */
const MIN_POST_MS = 1_000

/** A PostCost over the backend's CRUD route; `fetchImpl` carries the service credential (withServiceCredential). */
export function crudCostPoster(backendUrl: string, fetchImpl: typeof fetch): PostCost {
  const base = backendUrl.replace(/\/+$/, '')
  return async (report, timeoutMs = REPORT_TIMEOUT_MS) => {
    try {
      const response = await fetchImpl(`${base}${ASSISTANT_COST_PATH(report.orderId)}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          session_id: report.sessionId,
          principal: report.principal,
          order_item_id: report.orderItemId,
          cost_usd: report.costUsd,
        }),
        signal: AbortSignal.timeout(timeoutMs),
      })
      if (response.ok) {
        const answer = (await response.json()) as { entry?: unknown }
        return answer.entry === null ? 'deferred' : 'recorded'
      }
      await response.body?.cancel()
      // Only the route's own (its CRUD header) 400, 404, 409 or 422 is final. A 401 or 403 (no
      // credential yet, a dead one), a 429, a 5xx, a proxy's answer or a missing route on an
      // older API is tried again.
      return FINAL_REFUSALS.has(response.status) && response.headers.get('x-scadbuddy-crud') === '1' ? 'refused' : 'failed'
    } catch {
      return 'failed'
    }
  }
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' && value.length <= 100 ? value : null
}

type Pending = { order_id: string; order_item_id: string; attempted_usd: number | null }
type Order = { id: string; items: string[]; attempted: number | null }

export type OrderCostsOptions = {
  /** The stores' pool: short queries only, never held across a post. */
  sql: Sql
  /** The lock client's own connections, which hold a session's report lock across its posts. */
  locks: LockClient
  post: PostCost
  onError?: (err: unknown) => void
  now?: () => number
  /** How long `record` holds a revision call up (db/bounded.ts, #1076). */
  waitMs?: number
  /** The most sessions one sweep reports. */
  sweepLimit?: number
}

export class OrderCosts implements TouchedSink {
  private readonly sql: Sql
  private readonly locks: LockClient
  private readonly post: PostCost
  private readonly onError: ((err: unknown) => void) | undefined
  private readonly now: () => number
  private readonly waitMs: number
  private readonly sweepLimit: number

  constructor(options: OrderCostsOptions) {
    this.sql = options.sql
    this.locks = options.locks
    this.post = options.post
    this.onError = options.onError
    this.now = options.now ?? Date.now
    this.waitMs = options.waitMs ?? RECORD_WAIT_MS
    this.sweepLimit = options.sweepLimit ?? SWEEP_LIMIT
  }

  /**
   * A revision that succeeded, unless its order was already reported in this session. Waits at
   * most `waitMs` for its row (#1076): a late write is reported and the call goes on. Never throws.
   */
  async record(call: TouchedCall): Promise<void> {
    if (!REVISION_TOOLS.has(call.tool.name) || call.ok === false) return
    const order = text(call.input.order_id)
    const item = text(call.input.item_id)
    if (!order || !item) return
    await waitAtMost(this.insert(call.sessionId, order, item), this.waitMs, 'the order cost row', (err) => this.onError?.(err))
  }

  private async insert(sessionId: string, order: string, item: string): Promise<void> {
    try {
      await this.sql`
        INSERT INTO ai_session_order_costs (session_id, order_id, order_item_id)
        SELECT ${sessionId}::uuid, ${order}, ${item}
        WHERE NOT EXISTS (SELECT 1 FROM ai_session_order_costs
                          WHERE session_id = ${sessionId}::uuid AND order_id = ${order} AND reported_at IS NOT NULL)
        ON CONFLICT DO NOTHING`
    } catch (err) {
      this.onError?.(err)
    }
  }

  /**
   * Sends each order still to report its share: the share an earlier report sent and heard no
   * answer for, else an equal part of the session's spend not yet carried or sent. The session's
   * lock is held on a lock-client connection (db.ts `createLockClient`), never one of the stores';
   * with every lock connection out, nothing is sent now and the next turn's end or the sweep
   * reports it. Never throws.
   */
  async report(sessionId: string): Promise<void> {
    let lease: LockLease | undefined
    // Unhealthy unless shown otherwise: a connection whose lock state is unknown is never reused.
    let healthy = false
    try {
      lease = await this.locks.take()
      if (!lease) return
      const key = `${LOCK_PREFIX}${sessionId}`
      const [lock] = await lease.sql<{ locked: boolean }[]>`SELECT pg_try_advisory_lock(hashtextextended(${key}, 0)) AS locked`
      // A report already running for the session (here or on another replica) sends what
      // this one would; what it leaves waits for the next turn's end or the sweep.
      if (!lock?.locked) {
        healthy = true
        return
      }
      try {
        await this.send(sessionId)
      } finally {
        await lease.sql`SELECT pg_advisory_unlock(hashtextextended(${key}, 0))`
        healthy = true
      }
    } catch (err) {
      this.onError?.(err)
    } finally {
      lease?.done(healthy)
    }
  }

  /**
   * Reports the sessions no turn is running that have had orders left to report for
   * SWEEP_AFTER, at most `sweepLimit` of them: those never tried first, then those tried
   * longest ago (`tried_at`), so sessions it cannot report never take every place. Never throws.
   */
  async sweep(): Promise<void> {
    try {
      const sessions = await this.sql<{ session_id: string }[]>`
        SELECT c.session_id FROM ai_session_order_costs c JOIN ai_sessions s ON s.id = c.session_id
        WHERE c.reported_at IS NULL AND c.at < now() - ${SWEEP_AFTER}::interval
          AND s.status NOT IN ('running', 'waiting_approval', 'waiting_input')
        GROUP BY c.session_id ORDER BY max(c.tried_at) NULLS FIRST, min(c.at) LIMIT ${this.sweepLimit}`
      for (const { session_id } of sessions) await this.report(session_id)
    } catch (err) {
      this.onError?.(err)
    }
  }

  private async send(sessionId: string): Promise<void> {
    const sql = this.sql
    const deadline = this.now() + REPORT_DEADLINE_MS
    const pending = await sql<Pending[]>`
      SELECT order_id, order_item_id, attempted_usd FROM ai_session_order_costs
      WHERE session_id = ${sessionId}::uuid AND reported_at IS NULL ORDER BY at, order_id, order_item_id`
    if (pending.length === 0) return
    await sql`UPDATE ai_session_order_costs SET tried_at = now() WHERE session_id = ${sessionId}::uuid AND reported_at IS NULL`
    const [row] = await sql<{ cost: number; carried: number; principal: string; over: boolean }[]>`
      SELECT s.cost_usd AS cost, s.owner_id AS principal,
             (s.status IN ('done', 'failed') OR (s.status = 'idle' AND s.updated_at < now() - ${IDLE_OVER_AFTER}::interval)) AS over,
             (SELECT COALESCE(SUM(COALESCE(reported_usd, 0) + COALESCE(attempted_usd, 0)), 0)
              FROM ai_session_order_costs WHERE session_id = s.id) AS carried
      FROM ai_sessions s WHERE s.id = ${sessionId}::uuid`
    if (!row) return
    const byId = new Map<string, Order>()
    for (const p of pending) {
      const order = byId.get(p.order_id) ?? { id: p.order_id, items: [], attempted: null }
      order.items.push(p.order_item_id)
      if (p.attempted_usd !== null) order.attempted = (order.attempted ?? 0) + Number(p.attempted_usd)
      byId.set(p.order_id, order)
    }
    const orders = [...byId.values()]
    const fresh = orders.filter((o) => o.attempted === null)
    const share = fresh.length === 0 ? 0 : Math.max(Number(row.cost) - Number(row.carried), 0) / fresh.length
    // Under a hundredth of a cent no new share is sent: those orders stay to report, with the
    // next turn's spend. A larger share still under one minor unit of the order's currency is
    // sent, and the backend answers it with no entry (`deferred`), which keeps it the same way.
    // Once the session is over no more spend comes: such a share is closed out, reported with
    // nothing carried, so it leaves the sweep for good. Over is `done` or `failed`, or `idle`
    // with no session activity (`updated_at`) for IDLE_OVER_AFTER: a chat turn that ends well
    // leaves the session idle, and the sweep finds it there once that turn's own report has run.
    // Nothing carried means nothing lost: if the session works again, the closed-out spend is
    // still in `cost - carried`, so it folds into the next order's share.
    const over = row.over
    if (over && share < 1e-4) {
      for (const order of fresh) await this.closeOut(sessionId, order.id)
    }
    // The rows are read whole and updated by the items read: a revision recorded while the
    // report runs is no part of this share, and is marked reported with nothing carried.
    const due = share < 1e-4 ? orders.filter((o) => o.attempted !== null) : orders
    for (const order of due) {
      const left = deadline - this.now()
      if (left < MIN_POST_MS) return
      const amount = order.attempted ?? share
      if (order.attempted === null) {
        await sql`
          UPDATE ai_session_order_costs SET attempted_usd = ${amount / order.items.length}
          WHERE session_id = ${sessionId}::uuid AND order_id = ${order.id} AND order_item_id = ANY(${order.items})
            AND reported_at IS NULL`
      }
      const answer = await this.post(
        {
          orderId: order.id,
          orderItemId: order.items.length === 1 ? order.items[0]! : null,
          sessionId,
          principal: row.principal,
          costUsd: amount.toFixed(8),
        },
        Math.min(REPORT_TIMEOUT_MS, left),
      )
      if (answer === 'failed') continue
      if (answer === 'deferred') {
        if (over) await this.closeOut(sessionId, order.id)
        else {
          await sql`
            UPDATE ai_session_order_costs SET attempted_usd = NULL
            WHERE session_id = ${sessionId}::uuid AND order_id = ${order.id} AND reported_at IS NULL`
        }
        continue
      }
      await sql`
        UPDATE ai_session_order_costs
        SET reported_at = now(), reported_usd = COALESCE(attempted_usd, 0), attempted_usd = NULL
        WHERE session_id = ${sessionId}::uuid AND order_id = ${order.id} AND reported_at IS NULL`
    }
  }

  /** An order's share that can never be written: reported, nothing carried, out of the sweep. */
  private async closeOut(sessionId: string, orderId: string): Promise<void> {
    await this.sql`
      UPDATE ai_session_order_costs SET reported_at = now(), reported_usd = 0, attempted_usd = NULL
      WHERE session_id = ${sessionId}::uuid AND order_id = ${orderId} AND reported_at IS NULL`
  }
}

/** One TouchedSink that reports each call to every sink in turn. */
export function touchedSinks(...sinks: TouchedSink[]): TouchedSink {
  return { record: async (call) => void (await Promise.all(sinks.map((s) => s.record(call)))) }
}
