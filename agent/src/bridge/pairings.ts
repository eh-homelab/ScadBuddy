import { createHash, randomInt } from 'node:crypto'
import { CrudReadError } from '../api/crudRead.js'
import { chunks, type CrudTarget, crudOf, qs, segment, TABS_PER_CALL, UUID_SEGMENT } from '../api/crudStores.js'
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
// codes deny the request. The API keeps them (spec §9), in `ai_browser_pairings`
// (backend/scadbuddy/migrations_agent/20260929T1330Z_browser_pairings.sql).

/** How long a pairing request can be accepted. */
export const REQUEST_TTL_MS = 5 * 60_000
/** How long an accepted pairing lasts: a working day. */
export const PAIRED_TTL_MS = 8 * 60 * 60_000
/** Wrong codes before a request is denied. */
export const MAX_ATTEMPTS = 5
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

/** What the tab shows for a principal that is not a named token. */
export function fallbackLabel(principal: Pick<Principal, 'kind' | 'id' | 'clientIp'>): string {
  switch (principal.kind) {
    case 'oidc':
      return `OIDC user ${principal.id.replace(/^oidc:/, '')}`
    case 'anonymous':
      return `An MCP client without a token${principal.clientIp ? ` at ${principal.clientIp}` : ''}`
    default:
      return `${principal.kind} ${principal.id}`
  }
}

type WirePairing = { id: string; label: string; expires_at: string }
type WirePairedTab = WirePairing & { tab_id: string }

function fromWire(p: WirePairing): PairingView {
  return { id: p.id, label: p.label, expiresAt: new Date(p.expires_at) }
}

/** The route's reason for a request it refuses (tools/go/core/internal/stores `PAIRING_REFUSED`). */
const PAIRING_REFUSED = 'pairing-refused'

/**
 * The pairing store through the API's CRUD routes, area `pairings` (backend
 * tools/go/core/internal/stores): the API runs each method's transaction, `request`'s cap check
 * under the same advisory lock and `accept` under the row's `FOR UPDATE`. The code never leaves
 * this process: the API is sent its SHA-256 only, and compares hashes.
 */
export class CrudPairingStore implements PairingStore {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async request(principal: Principal): Promise<PairingRequest> {
    if (principal.kind === 'browser') throw new PairingError('the browser user pairs a tab by chatting from it')
    const code = newPairingCode()
    const body = { principal_kind: principal.kind, principal_id: principal.id, fallback_label: fallbackLabel(principal), code_hash: hashCode(code) }
    try {
      const made = await this.#call<WirePairing>('a pairing request', '/pairings', { body })
      return { id: made.id, code, label: made.label, expiresAt: new Date(made.expires_at) }
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === PAIRING_REFUSED) throw new PairingError(err.detail)
      throw err
    }
  }

  async accept(id: string, code: string, tabId: string): Promise<AcceptResult> {
    if (!UUID_SEGMENT.test(id)) return { ok: false, reason: 'gone' }
    const answer = await this.#call<{
      result: 'ok' | 'wrong_code' | 'gone'
      pairing: WirePairing | null
      principal_kind: Principal['kind'] | null
      principal_id: string | null
      attempts_left: number | null
    }>('a pairing accepted', `/pairings/${segment(id, UUID_SEGMENT, 'a pairing id')}/accept`, { body: { code_hash: hashCode(code), tab_id: tabId } })
    if (answer.result === 'ok' && answer.pairing && answer.principal_kind && answer.principal_id) {
      return { ok: true, pairing: fromWire(answer.pairing), principal: { kind: answer.principal_kind, id: answer.principal_id } }
    }
    if (answer.result === 'wrong_code') return { ok: false, reason: 'wrong_code', attemptsLeft: answer.attempts_left ?? 0 }
    return { ok: false, reason: 'gone' }
  }

  async deny(id: string): Promise<boolean> {
    if (!UUID_SEGMENT.test(id)) return false
    return (await this.#call<{ done: boolean }>('a pairing denied', `/pairings/${segment(id, UUID_SEGMENT, 'a pairing id')}/deny`, { method: 'POST' })).done
  }

  async end(id: string, tabId: string): Promise<boolean> {
    if (!UUID_SEGMENT.test(id)) return false
    return (await this.#call<{ done: boolean }>('a pairing ended', `/pairings/${segment(id, UUID_SEGMENT, 'a pairing id')}/end`, { body: { tab_id: tabId } })).done
  }

  async pairedTab(principal: Pick<Principal, 'kind' | 'id'>, signal?: AbortSignal): Promise<(PairingView & { tabId: string }) | undefined> {
    // A browser principal is never paired (`request` refuses it); the route takes only an MCP kind.
    if (principal.kind === 'browser') return undefined
    const { pairing } = await this.#call<{ pairing: WirePairedTab | null }>(
      "a principal's paired tab",
      `/pairings/paired-tab${qs({ principal_kind: principal.kind, principal_id: principal.id })}`,
      { signal },
    )
    return pairing ? { ...fromWire(pairing), tabId: pairing.tab_id } : undefined
  }

  async pending(): Promise<PairingView[]> {
    return (await this.#call<{ pairings: WirePairing[] }>('the pending pairings', '/pairings/pending')).pairings.map(fromWire)
  }

  async pairedWith(tabIds: readonly string[]): Promise<Map<string, PairingView[]>> {
    const out = new Map<string, PairingView[]>()
    // In runs the route takes; each tab's pairings come in one run, so their order holds.
    for (const run of chunks(tabIds, TABS_PER_CALL)) {
      const { pairings } = await this.#call<{ pairings: WirePairedTab[] }>('the pairings of tabs', '/pairings/paired-with', { body: { tab_ids: run } })
      for (const p of pairings) out.set(p.tab_id, [...(out.get(p.tab_id) ?? []), fromWire(p)])
    }
    return out
  }
}
