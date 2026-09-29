import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Principal } from '../src/auth/principal.js'
import { PostgresTokenStore, principalFor } from '../src/auth/tokens.js'
import {
  hashCode,
  MAX_ATTEMPTS,
  MAX_PENDING_PER_PRINCIPAL,
  normaliseCode,
  PAIRED_TTL_MS,
  PairingError,
  PostgresPairingStore,
  REQUEST_TTL_MS,
} from '../src/bridge/pairings.js'
import type { Database } from '../src/db.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The pairing store (#254, spec §8.5; src/bridge/pairings.ts) against the
// agent's real schema: codes kept only as hashes, single use, short-lived,
// bounded tries, and one live pairing per principal.

const TAB = 'tab-aaaaaaaaaaaaaaaaaaaaaa'
const OTHER_TAB = 'tab-bbbbbbbbbbbbbbbbbbbbbb'
const anonymous: Principal = { id: 'anonymous:session-1', kind: 'anonymous', tiers: ['read', 'write'], clientIp: '10.1.2.3' }

describe.skipIf(!TEST_DATABASE_URL)(`browser pairings${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: PostgresPairingStore
  let agent: Principal

  // One schema for the file, emptied before each test: migrating is serialised
  // across the whole database (db/migrations.ts MIGRATION_LOCK), so a schema per
  // test would hold up every other file's migrations.
  beforeAll(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new PostgresPairingStore(db.sql)
  })
  beforeEach(async () => {
    await db.sql`TRUNCATE ai_browser_pairings, ai_mcp_tokens`
    const { record } = await new PostgresTokenStore(db.sql).mint({ name: 'laptop', tier: 'write' })
    agent = principalFor(record.id, 'write')
  })
  afterAll(async () => {
    await drop()
  })

  it('keeps only the hash of the code, names a token principal after its token, and lists the request', async () => {
    const request = await store.request(agent)
    expect(request.code).toMatch(/^[A-HJKMNP-TV-Z2-9]{4}-[A-HJKMNP-TV-Z2-9]{4}$/)
    expect(request.label).toBe('MCP token “laptop”')
    expect(request.expiresAt.getTime() - Date.now()).toBeLessThanOrEqual(REQUEST_TTL_MS)
    const [row] = await db.sql<{ code_hash: string; status: string; tab_id: string | null }[]>`
      SELECT code_hash, status, tab_id FROM ai_browser_pairings WHERE id = ${request.id}`
    expect(row).toEqual({ code_hash: hashCode(request.code), status: 'pending', tab_id: null })
    expect(JSON.stringify(await db.sql`SELECT * FROM ai_browser_pairings`)).not.toContain(request.code)
    expect(await store.pending()).toEqual([{ id: request.id, label: 'MCP token “laptop”', expiresAt: request.expiresAt }])
    expect(await store.pairedTab(agent)).toBeUndefined()
  })

  it('pairs the principal with the tab the code was typed into, once', async () => {
    const request = await store.request(agent)
    // Typed loosely: lower case, without the dash.
    const typed = request.code.toLowerCase().replace('-', ' ')
    expect(normaliseCode(typed)).toBe(request.code)
    const accepted = await store.accept(request.id, typed, TAB)
    expect(accepted).toMatchObject({ ok: true, principal: { kind: 'bearer', id: agent.id }, pairing: { id: request.id } })
    const paired = await store.pairedTab(agent)
    expect(paired).toMatchObject({ id: request.id, tabId: TAB })
    expect(paired!.expiresAt.getTime() - Date.now()).toBeGreaterThan(PAIRED_TTL_MS - 60_000)
    expect(await store.pending()).toEqual([])
    expect((await store.pairedWith([TAB, OTHER_TAB])).get(TAB)).toHaveLength(1)
    // The same code again, from any tab, finds nothing.
    expect(await store.accept(request.id, request.code, OTHER_TAB)).toEqual({ ok: false, reason: 'gone' })
    expect(await store.pairedTab(agent)).toMatchObject({ tabId: TAB })
  })

  it('counts wrong codes and denies the request after the last try', async () => {
    const request = await store.request(agent)
    for (let tries = 1; tries < MAX_ATTEMPTS; tries++) {
      expect(await store.accept(request.id, 'AAAA-AAAA', TAB)).toEqual({
        ok: false,
        reason: 'wrong_code',
        attemptsLeft: MAX_ATTEMPTS - tries,
      })
    }
    expect(await store.accept(request.id, 'AAAA-AAAA', TAB)).toEqual({ ok: false, reason: 'gone' })
    // Even the right code is too late now.
    expect(await store.accept(request.id, request.code, TAB)).toEqual({ ok: false, reason: 'gone' })
    expect(await store.pending()).toEqual([])
  })

  it('refuses an expired request', async () => {
    const request = await store.request(agent)
    await db.sql`UPDATE ai_browser_pairings SET expires_at = now() - interval '1 second' WHERE id = ${request.id}`
    expect(await store.accept(request.id, request.code, TAB)).toEqual({ ok: false, reason: 'gone' })
    expect(await store.pending()).toEqual([])
  })

  it('lets a newer pairing replace the old one, and the user end it from its own tab only', async () => {
    const first = await store.request(agent)
    await store.accept(first.id, first.code, TAB)
    const second = await store.request(agent)
    await store.accept(second.id, second.code, OTHER_TAB)
    expect(await store.pairedTab(agent)).toMatchObject({ id: second.id, tabId: OTHER_TAB })
    expect((await store.pairedWith([TAB])).get(TAB)).toBeUndefined()

    expect(await store.end(second.id, TAB)).toBe(false)
    expect(await store.end(second.id, OTHER_TAB)).toBe(true)
    expect(await store.pairedTab(agent)).toBeUndefined()
  })

  it('stops pairing at the end of the pairing lifetime', async () => {
    const request = await store.request(agent)
    await store.accept(request.id, request.code, TAB)
    await db.sql`UPDATE ai_browser_pairings SET expires_at = now() - interval '1 second' WHERE id = ${request.id}`
    expect(await store.pairedTab(agent)).toBeUndefined()
    expect((await store.pairedWith([TAB])).size).toBe(0)
  })

  it('denies a request the user turned down', async () => {
    const request = await store.request(agent)
    expect(await store.deny(request.id)).toBe(true)
    expect(await store.deny(request.id)).toBe(false)
    expect(await store.accept(request.id, request.code, TAB)).toEqual({ ok: false, reason: 'gone' })
  })

  it('caps the requests one principal may have waiting, and names one without a token', async () => {
    for (let i = 0; i < MAX_PENDING_PER_PRINCIPAL; i++) await store.request(anonymous)
    await expect(store.request(anonymous)).rejects.toBeInstanceOf(PairingError)
    // Someone else is not held up by it.
    expect((await store.request(agent)).label).toBe('MCP token “laptop”')
    const labels = (await store.pending()).map((p) => p.label)
    expect(labels).toContain('An MCP client without a token at 10.1.2.3')
  })

  it('holds the per-principal cap under concurrent requests (#731 review)', async () => {
    const results = await Promise.allSettled(
      Array.from({ length: MAX_PENDING_PER_PRINCIPAL + 4 }, () => store.request(anonymous)),
    )
    expect(results.filter((r) => r.status === 'fulfilled')).toHaveLength(MAX_PENDING_PER_PRINCIPAL)
    for (const r of results) if (r.status === 'rejected') expect(r.reason).toBeInstanceOf(PairingError)
  })

  it('never pairs the browser user by code, and ignores ids that are not uuids', async () => {
    await expect(store.request({ id: 'browser', kind: 'browser', tiers: ['read'] })).rejects.toBeInstanceOf(PairingError)
    expect(await store.accept('not-a-uuid', 'x', TAB)).toEqual({ ok: false, reason: 'gone' })
    expect(await store.deny('not-a-uuid')).toBe(false)
    expect(await store.end('not-a-uuid', TAB)).toBe(false)
  })

  it('enforces one live pairing per principal in the schema itself', async () => {
    const first = await store.request(agent)
    await store.accept(first.id, first.code, TAB)
    const second = await store.request(agent)
    await expect(
      db.sql`UPDATE ai_browser_pairings SET status = 'paired', tab_id = ${OTHER_TAB} WHERE id = ${second.id}`,
    ).rejects.toThrow(/ai_browser_pairings_one_per_principal/)
  })
})
