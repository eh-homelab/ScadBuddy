import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DEFAULT_MCP_AUTH, type McpAuthSettings } from '../src/auth/authenticate.js'
import { defaultOidcConfig, OIDC_SETTINGS_KEY, OidcProvider, SettingsOidcConfigRepo } from '../src/auth/oidc.js'
import { PostgresTokenStore } from '../src/auth/tokens.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { appFetch, connect, INGRESS, MCP_URL, testApp } from './helpers/mcp.js'
import { type FakeIdp, startFakeIdp } from './support/fakeIdp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The OIDC configuration in Postgres (#262): `ai_settings.mcp_oidc`, written by
// the Settings route and read per /mcp request the way main.ts wires it.

const PUBLIC_URL = 'https://scadbuddy.test'
const UI = { host: 'scadbuddy.test', origin: PUBLIC_URL, 'x-forwarded-proto': 'https', 'content-type': 'application/json' }

describe.skipIf(!TEST_DATABASE_URL)(
  `OIDC settings in ai_settings${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let idp: FakeIdp
    const clients: Client[] = []

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      idp = await startFakeIdp()
    })
    afterEach(async () => {
      await Promise.all(clients.splice(0).map((c) => c.close()))
      await idp.close()
      await drop()
    })

    it('round-trips the configuration as JSON in ai_settings, and reads a broken row as off', async () => {
      const problems: string[] = []
      const repo = new SettingsOidcConfigRepo(new SettingsStore(db.sql), (d) => problems.push(d))
      expect(await repo.get()).toBeUndefined()
      const config = { ...defaultOidcConfig(idp.issuer), enabled: true, tier_claim: 'groups', audience: 'scadbuddy' }
      await repo.put(config, { actor: { kind: 'system', id: 'test', label: 'test' }, surface: 'system' as const })
      expect(await repo.get()).toEqual(config)
      const [row] = await db.sql<{ value: unknown }[]>`SELECT value FROM ai_settings WHERE key = ${OIDC_SETTINGS_KEY}`
      expect(row?.value).toEqual(config)

      await db.sql`UPDATE ai_settings SET value = '{"enabled": true}'::jsonb WHERE key = ${OIDC_SETTINGS_KEY}`
      expect(await repo.get()).toBeUndefined()
      expect(problems).toHaveLength(1)
    })

    it('Settings enables OIDC in Postgres, and /mcp then takes both JWTs and Postgres bearer tokens', async () => {
      const settings = new SettingsStore(db.sql)
      const repo = new SettingsOidcConfigRepo(settings)
      const tokens = new PostgresTokenStore(db.sql)
      const provider = new OidcProvider()
      const { app } = testApp({
        tokens,
        // As main.ts: `oidc` while the stored configuration is enabled, `bearer` otherwise.
        authSettings: async (): Promise<McpAuthSettings> => {
          const oidc = await repo.get()
          return oidc?.enabled ? { ...DEFAULT_MCP_AUTH, mode: 'oidc', oidc } : DEFAULT_MCP_AUTH
        },
        mcp: { oidc: provider, publicUrl: PUBLIC_URL },
        deps: { database: { ping: db.ping, ready: db.ready }, mcpOidc: { repo, provider, publicUrl: PUBLIC_URL } },
      })
      const jwt = await idp.sign({ scope: 'scadbuddy:write' })

      // Before: bearer mode, no metadata, the JWT is not a known bearer token.
      expect((await appFetch(app)(`${PUBLIC_URL}/.well-known/oauth-protected-resource`)).status).toBe(404)
      expect((await appFetch(app, { headers: { authorization: `Bearer ${jwt}` } })(MCP_URL, { method: 'POST', body: '{}' })).status).toBe(401)

      const put = await appFetch(app, { address: INGRESS, headers: UI })(`${PUBLIC_URL}/api/v1/ai/mcp/oidc`, {
        method: 'PUT',
        body: JSON.stringify({ ...defaultOidcConfig(idp.issuer), enabled: true }),
      })
      expect(put.status).toBe(200)

      // After: metadata served, the JWT works, and so does a token from ai_mcp_tokens.
      const meta = await appFetch(app)(`${PUBLIC_URL}/.well-known/oauth-protected-resource`)
      expect(await meta.json()).toMatchObject({ authorization_servers: [idp.issuer] })
      const viaJwt = await connect(app, { headers: { authorization: `Bearer ${jwt}` } })
      clients.push(viaJwt)
      expect((await viaJwt.listTools()).tools.length).toBeGreaterThan(0)
      const { token } = await tokens.mint({ name: 'ci', tier: 'read' })
      const viaToken = await connect(app, { headers: { authorization: `Bearer ${token}` } })
      clients.push(viaToken)
      expect((await viaToken.listTools()).tools.length).toBeGreaterThan(0)

      // Switching it off in Settings takes effect on the next request.
      const off = await appFetch(app, { address: INGRESS, headers: UI })(`${PUBLIC_URL}/api/v1/ai/mcp/oidc`, {
        method: 'PUT',
        body: JSON.stringify({ ...defaultOidcConfig(idp.issuer), enabled: false }),
      })
      expect(off.status).toBe(200)
      expect((await appFetch(app, { headers: { authorization: `Bearer ${jwt}` } })(MCP_URL, { method: 'POST', body: '{}' })).status).toBe(401)
    })
  },
)
