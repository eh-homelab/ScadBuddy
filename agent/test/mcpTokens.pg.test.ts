import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { hashToken, PostgresTokenStore } from '../src/auth/tokens.js'
import type { Database } from '../src/db.js'
import { migrate } from '../src/db/migrations.js'
import type { McpTokenList, MintedMcpToken } from '../src/routes/mcpTokens.js'
import { appFetch, connect, INGRESS, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The Settings token routes (routes/mcpTokens.ts) over the real store on
// `ai_mcp_tokens`: what Settings mints is what /mcp accepts, and a revoke from
// Settings shuts /mcp to it at once.

const URL_BASE = 'https://scadbuddy.test/api/v1/ai/mcp-tokens'
const UI = { origin: 'https://scadbuddy.test', 'x-forwarded-proto': 'https' }

describe.skipIf(!TEST_DATABASE_URL)(
  `/api/v1/ai/mcp-tokens on Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
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

    function app() {
      const tokens = new PostgresTokenStore(db.sql)
      const made = testApp({ tokens, deps: { tokens } })
      return { app: made.app, fetch: appFetch(made.app, { address: INGRESS, headers: UI }) }
    }

    it('mints through Settings, stores only the hash, and /mcp accepts the token until it is revoked', async () => {
      const { app: agent, fetch } = app()
      const res = await fetch(URL_BASE, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name: 'desktop', tier: 'write', expires_in: 86400 }),
      })
      expect(res.status).toBe(201)
      const minted = (await res.json()) as MintedMcpToken

      const rows = await db.sql<{ id: string; token_hash: string; expires_at: Date; created_at: Date }[]>`
        SELECT id, token_hash, expires_at, created_at FROM ai_mcp_tokens`
      expect(rows).toHaveLength(1)
      expect(rows[0]?.id).toBe(minted.record.id)
      expect(rows[0]?.token_hash).toBe(hashToken(minted.token))
      expect(rows[0]?.expires_at.toISOString()).toBe(minted.record.expires_at)
      const [whole] = await db.sql<{ t: string }[]>`SELECT t::text AS t FROM ai_mcp_tokens t`
      expect(whole?.t).not.toContain(minted.token)

      const auth = { headers: { authorization: `Bearer ${minted.token}` } }
      const client = await connect(agent, auth)
      await client.close()

      const listed = (await (await fetch(URL_BASE)).json()) as McpTokenList
      expect(listed.tokens).toHaveLength(1)
      expect(listed.tokens[0]).toMatchObject({ id: minted.record.id, name: 'desktop', tier: 'write', status: 'active' })
      expect(listed.tokens[0]?.last_used_at).not.toBeNull()
      expect(JSON.stringify(listed)).not.toContain(rows[0]!.token_hash)

      expect((await fetch(`${URL_BASE}/${minted.record.id}`, { method: 'DELETE' })).status).toBe(204)
      await expect(connect(agent, auth)).rejects.toThrow(/unknown, expired or revoked/)
      const after = (await (await fetch(URL_BASE)).json()) as McpTokenList
      expect(after.tokens[0]?.status).toBe('revoked')

      // A second revoke, a uuid never minted, and an id that is not a uuid are all 404, not 500.
      expect((await fetch(`${URL_BASE}/${minted.record.id}`, { method: 'DELETE' })).status).toBe(404)
      expect((await fetch(`${URL_BASE}/00000000-0000-4000-8000-000000000000`, { method: 'DELETE' })).status).toBe(404)
      expect((await fetch(`${URL_BASE}/not-a-uuid`, { method: 'DELETE' })).status).toBe(404)
    })

    it('lists newest first', async () => {
      const { fetch } = app()
      for (const name of ['first', 'second', 'third']) {
        const res = await fetch(URL_BASE, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ name, tier: 'read' }),
        })
        expect(res.status).toBe(201)
      }
      const listed = (await (await fetch(URL_BASE)).json()) as McpTokenList
      expect(listed.tokens.map((t) => t.name)).toEqual(['third', 'second', 'first'])
    })
  },
)
