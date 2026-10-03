import { randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AuditEntry } from '../src/audit/log.js'
import { CredentialError, CredentialStore, credentialAad } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { migrate, MIGRATIONS } from '../src/db/migrations.js'
import { CredentialPool, NoUsableCredentialError } from '../src/harness/fallback.js'
import { kekFromBase64, type KekStatus, sealSecret } from '../src/secrets.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #1093: several credentials in `ai_credentials`, their order and their
// health, against a real Postgres (the in-memory double is
// test/support/memoryCredentials.ts).

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const kekStatus: KekStatus = { ok: true, kek }
const KEY_A = 'sk-ant-api03-first-credential-aaaa'
const KEY_B = 'sk-ant-api03-second-credential-bbbb'
const GW = 'gw-third-credential-cccc'
const MIGRATION = '20261003T1617Z_credential_priority'

describe.skipIf(!TEST_DATABASE_URL)(
  `several Claude credentials in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let store: CredentialStore

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      store = new CredentialStore(db.sql)
    })
    afterEach(async () => {
      await drop()
    })

    it('migrates the single credential to priority 0, active, and it still opens', async () => {
      await migrate(db.sql, MIGRATIONS.filter((m) => m.id < MIGRATION))
      const envelope = sealSecret(kek, KEY_A, credentialAad('default', 'anthropic_api_key', null))
      await db.sql`
        INSERT INTO ai_credentials (id, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
        VALUES ('default', 'anthropic_api_key', NULL, ${envelope.secretSealed}, ${envelope.dekSealed}, ${envelope.kekId}, 'aaaa')`
      expect(await migrate(db.sql)).toContain(MIGRATION)
      expect(await store.list()).toMatchObject([
        { id: 'default', priority: 0, status: 'active', cooldown_until: null, last_error: null, epoch: 0 },
      ])
      expect(await store.reveal(kek)).toEqual({ kind: 'anthropic_api_key', secret: KEY_A })
    })

    describe('after migrations', () => {
      beforeEach(async () => {
        await migrate(db.sql)
      })

      it('creates in priority order, reveals each by id, and the single-credential routes act on the first', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        const b = await store.create({ kind: 'anthropic_api_key', secret: KEY_B }, kek)
        const c = await store.create({ kind: 'gateway', base_url: 'https://llm.example/', secret: GW }, kek)
        expect((await store.list()).map((x) => [x.id, x.priority])).toEqual([
          [a.id, 0],
          [b.id, 1],
          [c.id, 2],
        ])
        expect(await store.reveal(kek, b.id)).toEqual({ kind: 'anthropic_api_key', secret: KEY_B })
        expect(await store.reveal(kek, c.id)).toEqual({ kind: 'gateway', baseUrl: 'https://llm.example', secret: GW })
        expect(await store.get()).toMatchObject({ id: a.id })
        expect(await store.reveal(kek)).toEqual({ kind: 'anthropic_api_key', secret: KEY_A })
        expect(JSON.stringify(await store.list())).not.toMatch(/credential-(aaaa|bbbb)|third/)
      })

      it('binds each secret to its row: a sealed secret copied onto another row does not open', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        const b = await store.create({ kind: 'anthropic_api_key', secret: KEY_B }, kek)
        await db.sql`
          UPDATE ai_credentials SET secret_sealed = s.secret_sealed, dek_sealed = s.dek_sealed
          FROM (SELECT secret_sealed, dek_sealed FROM ai_credentials WHERE id = ${a.id}) s WHERE id = ${b.id}`
        await expect(store.reveal(kek, b.id)).rejects.toThrow()
      })

      it('creates the first credential through the single-credential put, and refuses an unknown id', async () => {
        const first = await store.put({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        expect(first).toMatchObject({ priority: 0, status: 'active' })
        await expect(store.put({ kind: 'anthropic_api_key', secret: KEY_B }, kek, 'nope')).rejects.toMatchObject({
          status: 404,
        })
        await expect(store.create({ kind: 'anthropic_api_key' }, kek)).rejects.toBeInstanceOf(CredentialError)
      })

      it('creates concurrently without two credentials at one priority', async () => {
        await Promise.all(
          Array.from({ length: 6 }, (_, i) => store.create({ kind: 'anthropic_api_key', secret: `${KEY_A}${i}` }, kek)),
        )
        expect((await store.list()).map((x) => x.priority)).toEqual([0, 1, 2, 3, 4, 5])
      })

      it('reorders, refusing an order that does not name every credential once', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        const b = await store.create({ kind: 'anthropic_api_key', secret: KEY_B }, kek)
        const c = await store.create({ kind: 'gateway', base_url: 'https://llm.example', secret: GW }, kek)
        const reordered = await store.reorder([c.id, a.id, b.id])
        expect(reordered.map((x) => [x.id, x.priority])).toEqual([
          [c.id, 0],
          [a.id, 1],
          [b.id, 2],
        ])
        for (const bad of [[c.id, a.id], [c.id, a.id, b.id, 'x'], [c.id, a.id, a.id], [c.id, a.id, 'x']]) {
          await expect(store.reorder(bad)).rejects.toMatchObject({ status: 409 })
        }
        expect((await store.list()).map((x) => x.id)).toEqual([c.id, a.id, b.id])
      })

      it('deletes, closing up the priorities; the single-credential delete removes the first', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        const b = await store.create({ kind: 'anthropic_api_key', secret: KEY_B }, kek)
        const c = await store.create({ kind: 'gateway', base_url: 'https://llm.example', secret: GW }, kek)
        expect(await store.delete(b.id)).toBe(true)
        expect((await store.list()).map((x) => [x.id, x.priority])).toEqual([
          [a.id, 0],
          [c.id, 1],
        ])
        expect(await store.delete()).toBe(true)
        expect(await store.list()).toMatchObject([{ id: c.id, priority: 0 }])
        expect(await store.delete('nope')).toBe(false)
      })

      it('disables, and a rate limit never downgrades a disabled credential', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        expect(await store.record(a.id, a.epoch, { kind: 'disabled', reason: 'HTTP 401: authentication_failed' })).toEqual({
          before: 'active',
          after: 'disabled',
          recovered: false,
        })
        const until = new Date(Date.now() + 60_000)
        expect(await store.record(a.id, a.epoch, { kind: 'cooling_down', until, reason: 'HTTP 429' })).toMatchObject({
          after: 'disabled',
        })
        expect(await store.get(a.id)).toMatchObject({ status: 'disabled', cooldown_until: null, last_error: 'HTTP 429' })
      })

      it('cools down, keeps the later of two cooldowns, and reads an expired one as active', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        const later = new Date(Date.now() + 120_000)
        await store.record(a.id, a.epoch, { kind: 'cooling_down', until: later, reason: 'HTTP 429' })
        await store.record(a.id, a.epoch, { kind: 'cooling_down', until: new Date(Date.now() + 30_000), reason: 'HTTP 429' })
        expect(await store.get(a.id)).toMatchObject({ status: 'cooling_down', cooldown_until: later.toISOString() })

        await db.sql`UPDATE ai_credentials SET cooldown_until = now() - interval '1 second' WHERE id = ${a.id}`
        expect(await store.get(a.id)).toMatchObject({ status: 'active', cooldown_until: null })
        // The first success after it clears the row and says it recovered.
        expect(await store.record(a.id, a.epoch, { kind: 'used' })).toEqual({
          before: 'active',
          after: 'active',
          recovered: true,
        })
        const [raw] = await db.sql<{ status: string; last_used_at: Date | null }[]>`
          SELECT status, last_used_at FROM ai_credentials WHERE id = ${a.id}`
        expect(raw).toMatchObject({ status: 'active' })
        expect(raw?.last_used_at).toBeInstanceOf(Date)
      })

      it('ignores a failure reported against an older epoch: a new secret or a reset wins', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        const saved = await store.put({ kind: 'anthropic_api_key', secret: KEY_B }, kek, a.id)
        expect(saved.epoch).toBe(a.epoch + 1)
        expect(await store.record(a.id, a.epoch, { kind: 'disabled', reason: 'old key revoked' })).toBeUndefined()
        expect(await store.get(a.id)).toMatchObject({ status: 'active' })

        await store.record(saved.id, saved.epoch, { kind: 'disabled', reason: 'HTTP 401' })
        const reset = await store.reset(saved.id)
        expect(reset).toMatchObject({ status: 'active', epoch: saved.epoch + 1, last_error: null, last_error_at: null })
        expect(await store.reset('nope')).toBeUndefined()
      })

      it('makes a disabled credential active again when a new secret is saved, but not when the secret is kept', async () => {
        const a = await store.create({ kind: 'gateway', base_url: 'https://llm.example', secret: GW }, kek)
        await store.record(a.id, a.epoch, { kind: 'disabled', reason: 'HTTP 403' })
        expect(await store.put({ kind: 'gateway', base_url: 'https://llm.example' }, kek, a.id)).toMatchObject({
          status: 'disabled',
          last_error: 'HTTP 403',
        })
        expect(await store.put({ kind: 'gateway', base_url: 'https://llm.example', secret: `${GW}2` }, kek, a.id)).toMatchObject(
          { status: 'active', last_error: null, last_error_at: null },
        )
      })

      it('races: two pods disabling and cooling one credential at once end disabled', async () => {
        const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
        const until = new Date(Date.now() + 60_000)
        const results = await Promise.all([
          store.record(a.id, a.epoch, { kind: 'cooling_down', until, reason: 'HTTP 429' }),
          store.record(a.id, a.epoch, { kind: 'disabled', reason: 'HTTP 401' }),
          store.record(a.id, a.epoch, { kind: 'cooling_down', until, reason: 'HTTP 429' }),
        ])
        expect(await store.get(a.id)).toMatchObject({ status: 'disabled' })
        // Exactly one report saw the change to disabled, so it is audited once.
        expect(results.filter((r) => r?.after === 'disabled' && r.before !== 'disabled')).toHaveLength(1)
      })

      describe('CredentialPool', () => {
        it('offers the usable credentials in order, and says when the first is back when none is', async () => {
          const audit: AuditEntry[] = []
          const pool = new CredentialPool({
            repo: store,
            kek: kekStatus,
            audit: { record: (e) => Promise.resolve(void audit.push(e)) },
            log: () => {},
          })
          await expect(pool.candidates()).rejects.toThrow(/no Claude credential is configured/)
          const a = await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, kek)
          const b = await store.create({ kind: 'gateway', base_url: 'https://llm.example', secret: GW }, kek)
          expect((await pool.candidates()).map((c) => c.credential.secret)).toEqual([KEY_A, GW])

          const [first, second] = await pool.candidates()
          const until = new Date(Date.now() + 90_000)
          await pool.report(first!, { class: 'rate_limited', reason: 'HTTP 429: rate_limit', until }, second, {
            sessionId: '00000000-0000-4000-8000-000000000001',
          })
          expect((await pool.candidates()).map((c) => c.id)).toEqual([b.id])
          await pool.report(second!, { class: 'permanent', reason: 'HTTP 401: authentication_failed' }, undefined)

          const error = await pool.candidates().catch((e: unknown) => e)
          expect(error).toBeInstanceOf(NoUsableCredentialError)
          expect((error as NoUsableCredentialError).recoversAt?.toISOString()).toBe(until.toISOString())
          expect((error as Error).message).toMatch(/credential 1 \(API key …aaaa\) is rate limited until/)
          expect((error as Error).message).toMatch(/credential 2 \(gateway https:\/\/llm.example …cccc\) is disabled \(HTTP 401/)
          expect((error as Error).message).not.toContain(KEY_A)

          expect(audit.map((e) => [e.kind, e.action, e.surface])).toEqual([
            ['credential', 'cooldown', 'harness'],
            ['credential', 'fallback', 'harness'],
            ['credential', 'disable', 'harness'],
          ])
          expect(audit[1]?.detail).toMatch(/credential 1 .* failed \(rate_limited: HTTP 429: rate_limit\); this turn continues on credential 2/)
          expect(audit[0]?.sessionId).toBe('00000000-0000-4000-8000-000000000001')
          expect(JSON.stringify(audit)).not.toMatch(/credential-aaaa|third-credential/)

          // Recovery: the cooldown passes, the credential is offered first again, and its next success is audited.
          await db.sql`UPDATE ai_credentials SET cooldown_until = now() - interval '1 second' WHERE id = ${a.id}`
          const [back] = await pool.candidates()
          expect(back?.id).toBe(a.id)
          await pool.report(back!, { class: 'ok' }, undefined)
          expect(audit.at(-1)).toMatchObject({ action: 'recover', outcome: 'ok' })
        })

        it('skips a credential the mounted key cannot open', async () => {
          const other = kekFromBase64(randomBytes(32).toString('base64'))
          await store.create({ kind: 'anthropic_api_key', secret: KEY_A }, other)
          const b = await store.create({ kind: 'anthropic_api_key', secret: KEY_B }, kek)
          const pool = new CredentialPool({ repo: store, kek: kekStatus, log: () => {} })
          expect((await pool.candidates()).map((c) => c.id)).toEqual([b.id])
        })
      })
    })
  },
)
