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
// session records nothing more. A report that failed (the backend out of reach, a 5xx) stays
// for the next turn's end; one refused for good (a 4xx: the order gone, a currency no rate
// reaches) is dropped and carried none of the spend, which the next order's report gets.
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

/** `recorded` (written now or before), `refused` (a 4xx: never sent again), `failed` (sent again at the next report). */
export type ReportAnswer = 'recorded' | 'refused' | 'failed'
export type PostCost = (report: AssistantCostReport) => Promise<ReportAnswer>

export const ASSISTANT_COST_PATH = (orderId: string) => `/api/v1/internal/crud/agent/orders/${encodeURIComponent(orderId)}/assistant-cost`
/** How long one report waits for the backend: it runs at a turn's end, before the next turn. */
export const REPORT_TIMEOUT_MS = 5_000

/** A PostCost over the backend's CRUD route; `fetchImpl` carries the service credential (withServiceCredential). */
export function crudCostPoster(backendUrl: string, fetchImpl: typeof fetch, timeoutMs = REPORT_TIMEOUT_MS): PostCost {
  const base = backendUrl.replace(/\/+$/, '')
  return async (report) => {
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
      await response.body?.cancel()
      if (response.ok) return 'recorded'
      // A 4xx from the route itself (its CRUD header) is final; anything else, a proxy's or
      // a missing route on an older API, is tried again.
      return response.status < 500 && response.headers.get('x-scadbuddy-crud') === '1' ? 'refused' : 'failed'
    } catch {
      return 'failed'
    }
  }
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' && value.length <= 100 ? value : null
}

type Pending = { order_id: string; order_item_id: string }

export class OrderCosts implements TouchedSink {
  private readonly sql: Sql
  private readonly post: PostCost
  private readonly onError: ((err: unknown) => void) | undefined

  constructor(sql: Sql, post: PostCost, onError?: (err: unknown) => void) {
    this.sql = sql
    this.post = post
    this.onError = onError
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

  /** Sends the session's spend since its last report to each order still to report. Never throws. */
  async report(sessionId: string): Promise<void> {
    try {
      const pending = await this.sql<Pending[]>`
        SELECT order_id, order_item_id FROM ai_session_order_costs
        WHERE session_id = ${sessionId}::uuid AND reported_at IS NULL ORDER BY at, order_id, order_item_id`
      if (pending.length === 0) return
      const [row] = await this.sql<{ cost: number; reported: number; principal: string }[]>`
        SELECT s.cost_usd AS cost, s.owner_id AS principal,
               (SELECT COALESCE(SUM(reported_usd), 0) FROM ai_session_order_costs WHERE session_id = s.id) AS reported
        FROM ai_sessions s WHERE s.id = ${sessionId}::uuid`
      if (!row) return
      const orders = new Map<string, string[]>()
      for (const p of pending) orders.set(p.order_id, [...(orders.get(p.order_id) ?? []), p.order_item_id])
      const since = Math.max(Number(row.cost) - Number(row.reported), 0)
      const share = since / orders.size
      for (const [orderId, items] of orders) {
        // Under a hundredth of a cent nothing is sent (the backend would write nothing):
        // the order stays to report, with the next turn's spend.
        if (share < 1e-4) continue
        const answer = await this.post({
          orderId,
          orderItemId: items.length === 1 ? items[0]! : null,
          sessionId,
          principal: row.principal,
          costUsd: share.toFixed(8),
        })
        if (answer === 'failed') continue
        const carried = answer === 'recorded' ? share / items.length : 0
        await this.sql`
          UPDATE ai_session_order_costs SET reported_at = now(), reported_usd = ${carried}
          WHERE session_id = ${sessionId}::uuid AND order_id = ${orderId} AND reported_at IS NULL`
      }
    } catch (err) {
      this.onError?.(err)
    }
  }
}

/** One TouchedSink that reports each call to every sink in turn. */
export function touchedSinks(...sinks: TouchedSink[]): TouchedSink {
  return { record: async (call) => void (await Promise.all(sinks.map((s) => s.record(call)))) }
}
