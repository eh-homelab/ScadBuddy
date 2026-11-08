import type { Sql } from 'postgres'
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
// 409 a currency no rate reaches, 422 a body it cannot take) is dropped and carried none of
// the spend, which the next order's report gets. Any other failure (no credential yet or a
// dead one, 401 and 403; 429; a 5xx; the backend out of reach) keeps it, with the share it
// sent (`attempted_usd`), and the next report sends that same share again: an answer lost
// after the backend wrote the entry loses no spend. A share the backend found under one minor
// unit of the order's currency (it answers no entry) stays to report, with the next turn's
// spend. One turn's reports share one deadline (REPORT_DEADLINE_MS); what it leaves waits
// for the next turn's end. The finish activity does not wait for `report`, and a session runs
// one report at a time in this process.
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
 * `recorded` (written now or before), `refused` (404, 409 or 422 from the route: never sent
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
/** The route's refusals that no later try mends: the order gone, its currency, the body. */
const FINAL_REFUSALS: ReadonlySet<number> = new Set([404, 409, 422])
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
      // Only the route's own (its CRUD header) 404, 409 or 422 is final. A 401 or 403 (no
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

export class OrderCosts implements TouchedSink {
  private readonly sql: Sql
  private readonly post: PostCost
  private readonly onError: ((err: unknown) => void) | undefined
  private readonly now: () => number
  /** The sessions with a report running in this process. */
  private readonly running = new Set<string>()

  constructor(sql: Sql, post: PostCost, onError?: (err: unknown) => void, options: { now?: () => number } = {}) {
    this.sql = sql
    this.post = post
    this.onError = onError
    this.now = options.now ?? Date.now
  }

  /** A revision that succeeded, unless its order was already reported in this session. Never throws. */
  async record(call: TouchedCall): Promise<void> {
    if (!REVISION_TOOLS.has(call.tool.name) || call.ok === false) return
    const order = text(call.input.order_id)
    const item = text(call.input.item_id)
    if (!order || !item) return
    try {
      await this.sql`
        INSERT INTO ai_session_order_costs (session_id, order_id, order_item_id)
        SELECT ${call.sessionId}::uuid, ${order}, ${item}
        WHERE NOT EXISTS (SELECT 1 FROM ai_session_order_costs
                          WHERE session_id = ${call.sessionId}::uuid AND order_id = ${order} AND reported_at IS NOT NULL)
        ON CONFLICT DO NOTHING`
    } catch (err) {
      this.onError?.(err)
    }
  }

  /**
   * Sends each order still to report its share: the share an earlier report sent and heard no
   * answer for, else an equal part of the session's spend not yet carried or sent. Never throws.
   */
  async report(sessionId: string): Promise<void> {
    // A report already running for the session sends what this one would; what it leaves
    // waits for the next turn's end.
    if (this.running.has(sessionId)) return
    this.running.add(sessionId)
    try {
      await this.send(sessionId)
    } catch (err) {
      this.onError?.(err)
    } finally {
      this.running.delete(sessionId)
    }
  }

  private async send(sessionId: string): Promise<void> {
    const deadline = this.now() + REPORT_DEADLINE_MS
    const pending = await this.sql<Pending[]>`
      SELECT order_id, order_item_id, attempted_usd FROM ai_session_order_costs
      WHERE session_id = ${sessionId}::uuid AND reported_at IS NULL ORDER BY at, order_id, order_item_id`
    if (pending.length === 0) return
    const [row] = await this.sql<{ cost: number; carried: number; principal: string }[]>`
      SELECT s.cost_usd AS cost, s.owner_id AS principal,
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
    // Under a hundredth of a cent no new share is sent (the backend would write nothing): those
    // orders stay to report, with the next turn's spend.
    const due = share < 1e-4 ? orders.filter((o) => o.attempted !== null) : orders
    for (const order of due) {
      const left = deadline - this.now()
      if (left < MIN_POST_MS) return
      const amount = order.attempted ?? share
      if (order.attempted === null) {
        await this.sql`
          UPDATE ai_session_order_costs SET attempted_usd = ${amount / order.items.length}
          WHERE session_id = ${sessionId}::uuid AND order_id = ${order.id} AND reported_at IS NULL`
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
        await this.sql`
          UPDATE ai_session_order_costs SET attempted_usd = NULL
          WHERE session_id = ${sessionId}::uuid AND order_id = ${order.id} AND reported_at IS NULL`
        continue
      }
      await this.sql`
        UPDATE ai_session_order_costs
        SET reported_at = now(), reported_usd = ${answer === 'recorded' ? this.sql`COALESCE(attempted_usd, 0)` : 0}, attempted_usd = NULL
        WHERE session_id = ${sessionId}::uuid AND order_id = ${order.id} AND reported_at IS NULL`
    }
  }
}

/** One TouchedSink that reports each call to every sink in turn. */
export function touchedSinks(...sinks: TouchedSink[]): TouchedSink {
  return { record: async (call) => void (await Promise.all(sinks.map((s) => s.record(call)))) }
}
