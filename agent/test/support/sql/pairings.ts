import { randomUUID, timingSafeEqual } from 'node:crypto'
import type { Sql } from 'postgres'
import { UUID_SEGMENT } from '../../../src/api/crudStores.js'
import type { Principal } from '../../../src/auth/principal.js'
import {
  type AcceptResult,
  fallbackLabel,
  hashCode,
  MAX_ATTEMPTS,
  MAX_PENDING,
  MAX_PENDING_PER_PRINCIPAL,
  newPairingCode,
  PAIRED_TTL_MS,
  PairingError,
  type PairingRequest,
  type PairingStore,
  type PairingView,
  REQUEST_TTL_MS,
} from '../../../src/bridge/pairings.js'

// Test-only: the pairing store over SQL, for the tests that run a whole agent on a throwaway
// database. The agent itself goes through the API (src/bridge/pairings.ts CrudPairingStore).

/** The advisory-lock key `request()` serialises its cap check and insert on. */
const REQUEST_LOCK = 'scadbuddy.ai_browser_pairings.request'

type Row = { id: string; principal_label: string; expires_at: Date }

function viewOf(row: Row): PairingView {
  return { id: row.id, label: row.principal_label, expiresAt: row.expires_at }
}

/**
 * The pairing store on `ai_browser_pairings`. `accept` runs in one
 * transaction with the row locked, so a code is accepted at most once however
 * many tabs try it, and the one-live-pairing-per-principal index holds.
 */
export class PostgresPairingStore implements PairingStore {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async request(principal: Principal): Promise<PairingRequest> {
    if (principal.kind === 'browser') throw new PairingError('the browser user pairs a tab by chatting from it')
    const sql = this.#sql
    // Rows past their use a day ago go, so the table stays small without a sweeper.
    await sql`
      DELETE FROM ai_browser_pairings
       WHERE expires_at < now() - interval '1 day'
          OR (status IN ('denied', 'ended') AND COALESCE(ended_at, created_at) < now() - interval '1 day')`
    const label = await this.#label(principal)
    const code = newPairingCode()
    // Count and insert under one transaction-scoped advisory lock, so two
    // concurrent requests cannot both read the counts below a cap and both
    // insert (#731 review); the lock is held only for these two statements.
    const row = await sql.begin(async (tx) => {
      await tx`SELECT pg_advisory_xact_lock(hashtextextended(${REQUEST_LOCK}, 0))`
      const [counts] = await tx<{ mine: number; all: number }[]>`
        SELECT count(*) FILTER (WHERE principal_kind = ${principal.kind} AND principal_id = ${principal.id})::int AS mine,
               count(*)::int AS all
          FROM ai_browser_pairings
         WHERE status = 'pending' AND expires_at > now()`
      if ((counts?.mine ?? 0) >= MAX_PENDING_PER_PRINCIPAL) {
        throw new PairingError(
          `this caller already has ${MAX_PENDING_PER_PRINCIPAL} pairing requests waiting; ask the user to answer one, or wait for them to expire`,
        )
      }
      if ((counts?.all ?? 0) >= MAX_PENDING) {
        throw new PairingError('too many pairing requests are waiting for the user; try again in a few minutes')
      }
      const [inserted] = await tx<Row[]>`
        INSERT INTO ai_browser_pairings (id, principal_kind, principal_id, principal_label, code_hash, status, expires_at)
        VALUES (${randomUUID()}, ${principal.kind}, ${principal.id}, ${label}, ${hashCode(code)}, 'pending',
                now() + (${REQUEST_TTL_MS} * interval '1 millisecond'))
        RETURNING id, principal_label, expires_at`
      return inserted!
    })
    return { id: row.id, code, label, expiresAt: row.expires_at }
  }

  /** A bearer principal is named after its token (`token:<uuid>`, auth/tokens.ts `principalFor`). */
  async #label(principal: Principal): Promise<string> {
    const tokenId = principal.kind === 'bearer' ? principal.id.replace(/^token:/, '') : ''
    if (UUID_SEGMENT.test(tokenId)) {
      const [token] = await this.#sql<{ name: string }[]>`SELECT name FROM ai_mcp_tokens WHERE id = ${tokenId}`
      if (token) return `MCP token “${token.name}”`
    }
    return fallbackLabel(principal)
  }

  async accept(id: string, code: string, tabId: string): Promise<AcceptResult> {
    if (!UUID_SEGMENT.test(id)) return { ok: false, reason: 'gone' }
    return this.#sql.begin(async (tx) => {
      const [row] = await tx<(Row & { code_hash: string; attempts: number; principal_kind: Principal['kind']; principal_id: string })[]>`
        SELECT id, principal_kind, principal_id, principal_label, code_hash, attempts, expires_at
          FROM ai_browser_pairings
         WHERE id = ${id} AND status = 'pending' AND expires_at > now()
           FOR UPDATE`
      if (!row) return { ok: false, reason: 'gone' } as const
      const typed = Buffer.from(hashCode(code), 'hex')
      if (!timingSafeEqual(typed, Buffer.from(row.code_hash, 'hex'))) {
        const attempts = row.attempts + 1
        await tx`
          UPDATE ai_browser_pairings
             SET attempts = ${attempts},
                 status = CASE WHEN ${attempts} >= ${MAX_ATTEMPTS} THEN 'denied' ELSE status END,
                 ended_at = CASE WHEN ${attempts} >= ${MAX_ATTEMPTS} THEN now() ELSE ended_at END
           WHERE id = ${id}`
        return attempts >= MAX_ATTEMPTS
          ? ({ ok: false, reason: 'gone' } as const)
          : ({ ok: false, reason: 'wrong_code', attemptsLeft: MAX_ATTEMPTS - attempts } as const)
      }
      // A newer pairing replaces the principal's old one (one tab per principal).
      await tx`
        UPDATE ai_browser_pairings SET status = 'ended', ended_at = now()
         WHERE principal_kind = ${row.principal_kind} AND principal_id = ${row.principal_id} AND status = 'paired'`
      const [paired] = await tx<Row[]>`
        UPDATE ai_browser_pairings
           SET status = 'paired', tab_id = ${tabId}, paired_at = now(),
               expires_at = now() + (${PAIRED_TTL_MS} * interval '1 millisecond')
         WHERE id = ${id}
        RETURNING id, principal_label, expires_at`
      return {
        ok: true,
        pairing: viewOf(paired!),
        principal: { kind: row.principal_kind, id: row.principal_id },
      } as const
    })
  }

  async deny(id: string): Promise<boolean> {
    if (!UUID_SEGMENT.test(id)) return false
    const rows = await this.#sql`
      UPDATE ai_browser_pairings SET status = 'denied', ended_at = now()
       WHERE id = ${id} AND status = 'pending' RETURNING id`
    return rows.length > 0
  }

  async end(id: string, tabId: string): Promise<boolean> {
    if (!UUID_SEGMENT.test(id)) return false
    const rows = await this.#sql`
      UPDATE ai_browser_pairings SET status = 'ended', ended_at = now()
       WHERE id = ${id} AND status = 'paired' AND tab_id = ${tabId} RETURNING id`
    return rows.length > 0
  }

  async pairedTab(principal: Pick<Principal, 'kind' | 'id'>, signal?: AbortSignal): Promise<(PairingView & { tabId: string }) | undefined> {
    signal?.throwIfAborted()
    const query = this.#sql<(Row & { tab_id: string })[]>`
      SELECT id, principal_label, expires_at, tab_id FROM ai_browser_pairings
       WHERE principal_kind = ${principal.kind} AND principal_id = ${principal.id}
         AND status = 'paired' AND expires_at > now()`
    // An abort stops waiting for the read; it never cancels it on the server. A
    // Postgres CancelRequest names the backend, not the query, so one that lands
    // after this cheap indexed read finished would cancel whatever statement the
    // pool handed that connection next (#1405 review). The read finishes and
    // returns its connection on its own.
    const [row] = signal ? await settledOrAborted(query, signal) : await query
    return row ? { ...viewOf(row), tabId: row.tab_id } : undefined
  }

  async pending(): Promise<PairingView[]> {
    const rows = await this.#sql<Row[]>`
      SELECT id, principal_label, expires_at FROM ai_browser_pairings
       WHERE status = 'pending' AND expires_at > now()
       ORDER BY created_at, id`
    return rows.map(viewOf)
  }

  async pairedWith(tabIds: readonly string[]): Promise<Map<string, PairingView[]>> {
    const out = new Map<string, PairingView[]>()
    if (tabIds.length === 0) return out
    const rows = await this.#sql<(Row & { tab_id: string })[]>`
      SELECT id, principal_label, expires_at, tab_id FROM ai_browser_pairings
       WHERE status = 'paired' AND expires_at > now() AND tab_id IN ${this.#sql(tabIds)}
       ORDER BY paired_at, id`
    for (const row of rows) out.set(row.tab_id, [...(out.get(row.tab_id) ?? []), viewOf(row)])
    return out
  }
}


/** `work`, unless `signal` aborts first: then its reason, and `work` is left to settle unobserved. */
function settledOrAborted<T>(work: PromiseLike<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason)
    signal.addEventListener('abort', abort, { once: true })
    Promise.resolve(work)
      .then(resolve, reject)
      .finally(() => signal.removeEventListener('abort', abort))
  })
}
