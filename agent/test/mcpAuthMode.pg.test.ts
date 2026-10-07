import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mcpAuthSettings } from '../src/auth/authenticate.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { appFetch, connect, INGRESS, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, migrate } from './support/postgres.js'

// /api/v1/ai/mcp/auth over the real `ai_settings` rows (#526's keys), wired as
// main.ts wires it: what Settings saves is what /mcp reads on its next request,
// on this replica and any other.

const URL_BASE = 'https://scadbuddy.test/api/v1/ai/mcp/auth'
const UI = { origin: 'https://scadbuddy.test', 'x-forwarded-proto': 'https' }

describe.skipIf(!TEST_DATABASE_URL)(
  `/api/v1/ai/mcp/auth on Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      await migrate(db.sql)
    })
    afterEach(async () => {
      await drop()
    })

    function replica() {
      const settings = new SettingsStore(db.sql)
      const made = testApp({ authSettings: mcpAuthSettings(settings, () => {}), deps: { aiSettings: settings } })
      return { app: made.app, fetch: appFetch(made.app, { address: INGRESS, headers: UI }) }
    }

    function put(fetcher: typeof fetch, body: unknown) {
      return fetcher(URL_BASE, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      })
    }

    it('stores both keys and every replica’s /mcp follows them from the next request', async () => {
      const one = replica()
      const two = replica()
      expect(await (await one.fetch(URL_BASE)).json()).toEqual({
        mode: 'bearer',
        configured_mode: 'bearer',
        anonymous_cap: 'outward',
      })
      await expect(connect(two.app)).rejects.toThrow(/bearer token is required/)

      const defaults = { mode: 'bearer', anonymous_cap: 'outward' }
      expect((await put(one.fetch, { mode: 'disabled', anonymous_cap: 'write', expected: defaults })).status).toBe(200)
      const rows = await db.sql<{ key: string; value: unknown }[]>`
        SELECT key, value FROM ai_settings WHERE key LIKE 'mcp_%' ORDER BY key`
      expect(rows).toEqual([
        { key: 'mcp_anonymous_cap', value: 'write' },
        { key: 'mcp_auth_mode', value: 'disabled' },
      ])
      expect(await (await two.fetch(URL_BASE)).json()).toEqual({
        mode: 'disabled',
        configured_mode: 'disabled',
        anonymous_cap: 'write',
      })
      const client = await connect(two.app)
      await client.close()

      const off = { mode: 'disabled', anonymous_cap: 'write' }
      expect((await put(two.fetch, { mode: 'bearer', anonymous_cap: 'write', expected: off })).status).toBe(200)
      await expect(connect(one.app)).rejects.toThrow(/bearer token is required/)

      // Replica one's page still shows `disabled`; its save is refused rather than turning auth off again.
      const stale = await put(one.fetch, { mode: 'disabled', anonymous_cap: 'read', expected: off })
      expect(stale.status).toBe(409)
      expect(await settingsRow('mcp_auth_mode')).toBe('bearer')
    })

    async function settingsRow(key: string): Promise<unknown> {
      const [row] = await db.sql<{ value: unknown }[]>`SELECT value FROM ai_settings WHERE key = ${key}`
      return row?.value
    }

    it('compare-and-set: a check that fails writes nothing, and concurrent writers are serialised', async () => {
      const settings = new SettingsStore(db.sql)
      expect(await settings.setMany({ mcp_auth_mode: 'disabled' }, async () => false)).toBe(false)
      expect(await settingsRow('mcp_auth_mode')).toBeUndefined()

      // Two compare-and-sets that both expect the key unset: exactly one wins.
      const expectUnset = async (current: { get<T>(key: string): Promise<T | undefined> }) =>
        (await current.get('mcp_auth_mode')) === undefined
      const results = await Promise.all([
        settings.setMany({ mcp_auth_mode: 'disabled' }, expectUnset),
        settings.setMany({ mcp_auth_mode: 'bearer' }, expectUnset),
      ])
      expect(results.filter(Boolean)).toHaveLength(1)
    })

    it('writes the mode and the cap in one transaction', async () => {
      const settings = new SettingsStore(db.sql)
      await settings.setMany({ mcp_auth_mode: 'bearer', mcp_anonymous_cap: 'outward' })
      // A value Postgres cannot store as JSON fails the second insert; the first must roll back with it.
      await expect(
        settings.setMany({ mcp_auth_mode: 'disabled', mcp_anonymous_cap: '\u0000' }, async () => true),
      ).rejects.toThrow()
      expect(await settings.get('mcp_auth_mode')).toBe('bearer')
      expect(await settings.get('mcp_anonymous_cap')).toBe('outward')
    })
  },
)
