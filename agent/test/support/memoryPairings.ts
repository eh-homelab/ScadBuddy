import { randomUUID } from 'node:crypto'
import type { Principal } from '../../src/auth/principal.js'
import {
  type AcceptResult,
  hashCode,
  MAX_ATTEMPTS,
  newPairingCode,
  PAIRED_TTL_MS,
  type PairingRequest,
  type PairingStore,
  type PairingView,
  REQUEST_TTL_MS,
} from '../../src/bridge/pairings.js'

type Row = {
  id: string
  principal: Pick<Principal, 'kind' | 'id'>
  label: string
  hash: string
  status: 'pending' | 'paired' | 'denied' | 'ended'
  tabId?: string
  attempts: number
  expiresAt: Date
}

/**
 * PostgresPairingStore's behaviour without Postgres, for the unit tests: the
 * same codes, hash, attempts and one-pairing-per-principal rule, kept in
 * memory. Test-only; the service keeps pairings in `ai_browser_pairings`
 * (covered by test/bridgePairings.pg.test.ts).
 */
export class InMemoryPairingStore implements PairingStore {
  readonly rows: Row[] = []

  async request(principal: Principal): Promise<PairingRequest> {
    const code = newPairingCode()
    const row: Row = {
      id: randomUUID(),
      principal: { kind: principal.kind, id: principal.id },
      label: `${principal.kind} ${principal.id}`,
      hash: hashCode(code),
      status: 'pending',
      attempts: 0,
      expiresAt: new Date(Date.now() + REQUEST_TTL_MS),
    }
    this.rows.push(row)
    return { id: row.id, code, label: row.label, expiresAt: row.expiresAt }
  }

  async accept(id: string, code: string, tabId: string): Promise<AcceptResult> {
    const row = this.rows.find((r) => r.id === id && r.status === 'pending' && r.expiresAt > new Date())
    if (!row) return { ok: false, reason: 'gone' }
    if (hashCode(code) !== row.hash) {
      row.attempts += 1
      if (row.attempts >= MAX_ATTEMPTS) row.status = 'denied'
      return row.status === 'denied' ? { ok: false, reason: 'gone' } : { ok: false, reason: 'wrong_code', attemptsLeft: MAX_ATTEMPTS - row.attempts }
    }
    for (const other of this.rows) {
      if (other.status === 'paired' && other.principal.kind === row.principal.kind && other.principal.id === row.principal.id) {
        other.status = 'ended'
      }
    }
    row.status = 'paired'
    row.tabId = tabId
    row.expiresAt = new Date(Date.now() + PAIRED_TTL_MS)
    return { ok: true, pairing: view(row), principal: row.principal }
  }

  async deny(id: string): Promise<boolean> {
    const row = this.rows.find((r) => r.id === id && r.status === 'pending')
    if (row) row.status = 'denied'
    return Boolean(row)
  }

  async end(id: string, tabId: string): Promise<boolean> {
    const row = this.rows.find((r) => r.id === id && r.status === 'paired' && r.tabId === tabId)
    if (row) row.status = 'ended'
    return Boolean(row)
  }

  async pairedTab(principal: Pick<Principal, 'kind' | 'id'>) {
    const row = this.rows.find(
      (r) => r.status === 'paired' && r.expiresAt > new Date() && r.principal.kind === principal.kind && r.principal.id === principal.id,
    )
    return row ? { ...view(row), tabId: row.tabId! } : undefined
  }

  async pending(): Promise<PairingView[]> {
    return this.rows.filter((r) => r.status === 'pending' && r.expiresAt > new Date()).map(view)
  }

  async pairedWith(tabIds: readonly string[]): Promise<Map<string, PairingView[]>> {
    const out = new Map<string, PairingView[]>()
    for (const row of this.rows) {
      if (row.status !== 'paired' || row.expiresAt <= new Date() || !tabIds.includes(row.tabId!)) continue
      out.set(row.tabId!, [...(out.get(row.tabId!) ?? []), view(row)])
    }
    return out
  }
}

function view(row: Row): PairingView {
  return { id: row.id, label: row.label, expiresAt: row.expiresAt }
}
