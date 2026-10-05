import { createHash, randomInt, randomUUID, timingSafeEqual } from 'node:crypto'
import type { Sql } from 'postgres'
import type { Principal } from '../auth/principal.js'

// Pairing an external agent with the user's tab (spec §8.5, #254). The
// browser user's own chat sessions pair with the tab they chat from on their
// own (bridge/hub.ts `pairSession`); anything else that wants to drive a tab,
// an MCP client in any auth mode, `disabled` included, needs the user to
// accept it IN the tab:
//
//   1. The agent calls `browser_pair` (tools/browser.ts). `request` stores a
//      pending row and hands back a short code, once. Only its SHA-256 is
//      kept, like the MCP bearer tokens (auth/tokens.ts).
//   2. Every connected tab shows the request, with who is asking (the token's
//      name) and a field for the code. The agent tells the user the code; the
//      user types it into the tab it should drive. Typing the code, not only
//      clicking Allow, is what shows the user is pairing the agent they are
//      talking to, as in the device authorization grant (RFC 8628 §3.3,
//      https://www.rfc-editor.org/rfc/rfc8628#section-3.3). The prompt is
//      user-only (frontend `USER_ONLY`), so a paired agent's own click and
//      fill cannot accept another one.
//   3. `accept` binds the principal to that tab until the user disconnects it,
//      the principal pairs again (a newer pairing replaces the old one), or
//      PAIRED_TTL_MS passes. A tab id lives as long as the page load (the
//      frontend keeps it in memory only), so a reload ends it in practice.
//
// The code is single-use and short-lived (REQUEST_TTL_MS); MAX_ATTEMPTS wrong
// codes deny the request. Persistence is Postgres only (spec §9), in
// `ai_browser_pairings` (db/migrations/20260929T1330Z_browser_pairings.sql).

/** How long a pairing request can be accepted. */
export const REQUEST_TTL_MS = 5 * 60_000
/** How long an accepted pairing lasts: a working day. */
export const PAIRED_TTL_MS = 8 * 60 * 60_000
/** Wrong codes before a request is denied. */
export const MAX_ATTEMPTS = 5
/** The advisory-lock key `request()` serialises its cap check and insert on. */
const REQUEST_LOCK = 'scadbuddy.ai_browser_pairings.request'
/** Pending requests one principal may have at once, and everyone together. */
export const MAX_PENDING_PER_PRINCIPAL = 3
export const MAX_PENDING = 20

/** No 0/O, 1/I/L or U, so a code read aloud or retyped is not misread (Crockford's base32 idea). */
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTVWXYZ23456789'
/** 8 symbols of 30: about 39 bits, for a code that lives five minutes and allows five tries. */
const CODE_LENGTH = 8

/** `ABCD-EFGH`, from the OS CSPRNG (`crypto.randomInt`). */
export function newPairingCode(): string {
  let code = ''
  for (let i = 0; i < CODE_LENGTH; i++) code += CODE_ALPHABET[randomInt(CODE_ALPHABET.length)]
  return `${code.slice(0, 4)}-${code.slice(4)}`
}

/** What the user typed, as the code was issued: upper case, the dash put back, spaces dropped. */
export function normaliseCode(typed: string): string {
  const bare = typed.toUpperCase().replace(/[\s-]/g, '')
  return bare.length === CODE_LENGTH ? `${bare.slice(0, 4)}-${bare.slice(4)}` : bare
}

export function hashCode(code: string): string {
  return createHash('sha256').update(normaliseCode(code), 'utf8').digest('hex')
}

/** A pairing as a tab lists it: never the code. */
export type PairingView = { id: string; label: string; expiresAt: Date }

export type PairingRequest = { id: string; code: string; label: string; expiresAt: Date }

export type AcceptResult =
  | { ok: true; pairing: PairingView; principal: Pick<Principal, 'kind' | 'id'> }
  | { ok: false; reason: 'wrong_code'; attemptsLeft: number }
  | { ok: false; reason: 'gone' }

export class PairingError extends Error {
  override name = 'PairingError'
}

export interface PairingStore {
  /** A new pending request for `principal`; the code is returned here once and never stored. */
  request(principal: Principal): Promise<PairingRequest>
  /** The user typed `code` for request `id` in tab `tabId`. */
  accept(id: string, code: string, tabId: string): Promise<AcceptResult>
  /** The user turned request `id` down. True when a pending one was. */
  deny(id: string): Promise<boolean>
  /** The user disconnected pairing `id` from tab `tabId`. True when a live one was. */
  end(id: string, tabId: string): Promise<boolean>
  /** The tab `principal` is paired with now, if any. `signal` cancels the query. */
  pairedTab(principal: Pick<Principal, 'kind' | 'id'>, signal?: AbortSignal): Promise<(PairingView & { tabId: string }) | undefined>
  /** Every request still waiting for a user, oldest first. */
  pending(): Promise<PairingView[]>
  /** The live pairings of these tabs, by tab id. */
  pairedWith(tabIds: readonly string[]): Promise<Map<string, PairingView[]>>
}

// Ids are uuid columns; anything else cannot match a row, and would make
// Postgres raise `invalid input syntax for type uuid` instead of answering.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** What the tab shows for a principal that is not a named token. */
function fallbackLabel(principal: Pick<Principal, 'kind' | 'id' | 'clientIp'>): string {
  switch (principal.kind) {
    case 'oidc':
      return `OIDC user ${principal.id.replace(/^oidc:/, '')}`
    case 'anonymous':
      return `An MCP client without a token${principal.clientIp ? ` at ${principal.clientIp}` : ''}`
    default:
      return `${principal.kind} ${principal.id}`
  }
}

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
    if (UUID.test(tokenId)) {
      const [token] = await this.#sql<{ name: string }[]>`SELECT name FROM ai_mcp_tokens WHERE id = ${tokenId}`
      if (token) return `MCP token “${token.name}”`
    }
    return fallbackLabel(principal)
  }

  async accept(id: string, code: string, tabId: string): Promise<AcceptResult> {
    if (!UUID.test(id)) return { ok: false, reason: 'gone' }
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
    if (!UUID.test(id)) return false
    const rows = await this.#sql`
      UPDATE ai_browser_pairings SET status = 'denied', ended_at = now()
       WHERE id = ${id} AND status = 'pending' RETURNING id`
    return rows.length > 0
  }

  async end(id: string, tabId: string): Promise<boolean> {
    if (!UUID.test(id)) return false
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
    // A cancelled query rejects and gives its pool slot (or queue place) back.
    // (postgres.js answers cancel() with a promise its types omit; a failed cancel request is not this read's error.)
    const cancel = () => void Promise.resolve(query.cancel() as unknown).catch(() => undefined)
    signal?.addEventListener('abort', cancel, { once: true })
    const [row] = await query.finally(() => signal?.removeEventListener('abort', cancel))
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
