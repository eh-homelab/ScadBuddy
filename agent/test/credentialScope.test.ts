import { randomBytes } from 'node:crypto'
import type { UpgradeWebSocket } from 'hono/ws'
import { describe, expect, it } from 'vitest'
import { aiStatus } from '../src/aiStatus.js'
import { createApp } from '../src/app.js'
import type { AuditEntry, AuditRepo } from '../src/audit/log.js'
import { agentUses, type Credential, CrudCredentials, type StoredCredential } from '../src/credentials.js'
import { CredentialPool, NoUsableCredentialError } from '../src/harness/fallback.js'
import { originPolicy } from '../src/http/origins.js'
import type { CredentialListView } from '../src/routes/credentials.js'
import { kekFromBase64 } from '../src/secrets.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// #7983: the shared pool holds the fleet's rows too. The agent uses only a row whose scope is
// `agent` or `both` and whose kind it can run; a `claude_login` row and a `fleet` row are the
// fleet's. Settings still lists them, with the scope.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const KEY = 'sk-ant-api03-scope-agent-aaaa'
const FLEET_KEY = 'sk-ant-api03-scope-fleet-ffff'
const BOTH_KEY = 'sk-ant-api03-scope-both-bbbb'
const LOGIN = JSON.stringify({ accessToken: 'sk-ant-oat01-login-llll', refreshToken: 'sk-ant-ort01-login-rrrr', expiresAt: 1795000000000 })

async function pool() {
  const repo = new MemoryCredentials(kek)
  const login = await repo.create({ kind: 'claude_login', secret: LOGIN })
  const fleet = await repo.create({ kind: 'anthropic_api_key', secret: FLEET_KEY, scope: 'fleet' })
  const agent = await repo.create({ kind: 'anthropic_api_key', secret: KEY })
  const both = await repo.create({ kind: 'anthropic_api_key', secret: BOTH_KEY, scope: 'both' })
  return { repo, login, fleet, agent, both }
}

describe('agentUses (#7983)', () => {
  it('is true only for a kind the agent runs, in scope agent or both', () => {
    expect(agentUses({ kind: 'anthropic_api_key', scope: 'agent' })).toBe(true)
    expect(agentUses({ kind: 'gateway', scope: 'both' })).toBe(true)
    expect(agentUses({ kind: 'claude_oauth_token', scope: 'fleet' })).toBe(false)
    expect(agentUses({ kind: 'claude_login', scope: 'both' })).toBe(false)
  })
})

describe("CredentialPool takes the agent's rows only (#7983)", () => {
  it('skips a claude_login and a fleet row, in priority order', async () => {
    const { repo, agent, both } = await pool()
    const found = await new CredentialPool({ repo, log: () => undefined }).candidates()
    expect(found.map((c) => c.id)).toEqual([agent.id, both.id])
    expect(found.map((c) => c.credential.secret)).toEqual([KEY, BOTH_KEY])
  })

  it('says no credential is configured when the pool holds only the fleet’s', async () => {
    const repo = new MemoryCredentials(kek)
    await repo.create({ kind: 'claude_login', secret: LOGIN })
    await repo.create({ kind: 'anthropic_api_key', secret: FLEET_KEY, scope: 'fleet' })
    const error = await new CredentialPool({ repo, log: () => undefined }).candidates().catch((err: unknown) => err)
    expect(error).toBeInstanceOf(NoUsableCredentialError)
    expect((error as Error).message).toBe('no Claude credential is configured')
  })

  it('/status says no Claude credential when only the fleet’s are stored', async () => {
    const repo = new MemoryCredentials(kek)
    await repo.create({ kind: 'claude_login', secret: LOGIN })
    expect((await aiStatus({ credentials: repo }, true)).ai).toBe('disabled (no Claude credential)')
  })
})

describe('CrudCredentials and the fleet’s rows (#7983)', () => {
  const wire = (over: Partial<Record<string, unknown>>) => ({
    id: 'c1', priority: 0, kind: 'anthropic_api_key', name: null, base_url: null, last4: 'aaaa',
    updated_at: '2026-11-26T00:00:00+00:00', status: 'active', cooldown_until: null, last_error: null,
    last_error_at: null, last_used_at: null, epoch: 0, kek_id: 'k', legacy_format: false, scope: 'agent',
    account_uuid: null, organization_uuid: null, subscription_type: null, rate_limit_tier: null, email: null,
    leased_to: null, leased_at: null, lease_epoch: 0, ...over,
  })

  // The API never answers the agent a fleet row (core's and the backend's tests); this is the
  // agent's second, defensive check.
  it('runs no claude_login an answer holds, and reads the scope and the account', async () => {
    const rows = [
      { stored: wire({ id: 'l1', kind: 'claude_login', scope: 'fleet', email: 'ops@example.com', subscription_type: 'max' }),
        credential: { kind: 'claude_login', base_url: null, secret: LOGIN } },
      { stored: wire({ id: 'k1' }), credential: { kind: 'anthropic_api_key', base_url: null, secret: KEY } },
    ]
    const fetch = () =>
      Promise.resolve(new Response(JSON.stringify({ credentials: rows }), { headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }))
    const revealed = await new CrudCredentials('http://api.test', { fetch }).revealAll()
    expect(revealed.map((r) => r.credential?.secret)).toEqual([undefined, KEY])
    const login = revealed[0]?.stored as StoredCredential
    expect([login.scope, login.email, login.subscription_type]).toEqual(['fleet', 'ops@example.com', 'max'])
  })
})

describe('the credential routes and the fleet’s rows (#7983)', () => {
  const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

  async function setup() {
    const rows = await pool()
    const tested: Credential[] = []
    const app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      credentials: rows.repo,
      testConnection: (credential) => {
        tested.push(credential)
        return Promise.resolve({ ok: true, detail: 'connected', duration_ms: 1, model: 'm' })
      },
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      resolveHost: () => Promise.resolve(['203.0.113.10']),
      testCooldownMs: 0,
      audit: { record: (e: AuditEntry) => Promise.resolve(void e) } as unknown as AuditRepo,
      sessions: {} as SessionManager,
      upgradeWebSocket: (() => () => Promise.resolve()) as unknown as UpgradeWebSocket,
    })
    return { ...rows, app, tested }
  }

  it('list every row with its scope', async () => {
    const { app } = await setup()
    const res = await app.request('/api/v1/ai/credentials/entries', { headers: UI })
    const body = (await res.json()) as CredentialListView
    expect(body.credentials.map((c) => [c.kind, c.scope])).toEqual([
      ['claude_login', 'fleet'],
      ['anthropic_api_key', 'fleet'],
      ['anthropic_api_key', 'agent'],
      ['anthropic_api_key', 'both'],
    ])
    expect(JSON.stringify(body)).not.toContain('login-rrrr')
  })

  it('count only the agent’s rows as usable', async () => {
    const { app, repo, agent, both } = await setup()
    for (const c of [agent, both]) await repo.record(c.id, c.epoch, { kind: 'disabled', reason: 'HTTP 401' })
    const res = await app.request('/api/v1/ai/credentials/entries', { headers: UI })
    expect(((await res.json()) as CredentialListView).usable_now).toBe(false)
  })

  it('refuse to test a row the agent does not use, and open nothing', async () => {
    const { app, login, fleet, both, tested } = await setup()
    for (const c of [login, fleet]) {
      const res = await app.request(`/api/v1/ai/credentials/entries/${c.id}/test`, { method: 'POST', headers: UI })
      expect(res.status).toBe(409)
    }
    expect(tested).toEqual([])
    const ok = await app.request(`/api/v1/ai/credentials/entries/${both.id}/test`, { method: 'POST', headers: UI })
    expect(ok.status).toBe(200)
    expect(tested.map((c) => c.secret)).toEqual([BOTH_KEY])
  })

  it('answer 404, and open nothing, for a row that went to the fleet between the check and the reveal', async () => {
    // The route's fleet check (`get`) and its `reveal` are two API calls; the reveal's own
    // filter is the boundary, so a row moved to `fleet` between them reads as unknown.
    const { app, repo, agent, tested } = await setup()
    const get = repo.get.bind(repo)
    let moved = false
    repo.get = async (id?: string) => {
      const before = await get(id)
      if (!moved && id === agent.id) {
        moved = true
        await repo.put({ kind: 'anthropic_api_key', scope: 'fleet' }, agent.id)
      }
      return before
    }
    const res = await app.request(`/api/v1/ai/credentials/entries/${agent.id}/test`, { method: 'POST', headers: UI })
    expect(res.status).toBe(404)
    expect(tested).toEqual([])
  })

  it('act on the first row the agent uses on the deprecated single-credential routes', async () => {
    const { app, repo, login, fleet, agent, both, tested } = await setup()
    const read = await app.request('/api/v1/ai/credentials', { headers: UI })
    expect(((await read.json()) as { last4: string }).last4).toBe('aaaa')
    const test = await app.request('/api/v1/ai/credentials/test', { method: 'POST', headers: UI })
    expect(test.status).toBe(200)
    expect(tested.map((c) => c.secret)).toEqual([KEY])
    const put = await app.request('/api/v1/ai/credentials', {
      method: 'PUT',
      headers: { ...UI, 'content-type': 'application/json' },
      body: JSON.stringify({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-scope-new-nnnn' }),
    })
    expect(put.status).toBe(200)
    const gone = await app.request('/api/v1/ai/credentials', { method: 'DELETE', headers: UI })
    expect(((await gone.json()) as { last4: string }).last4).toBe('bbbb')
    expect((await repo.list()).map((c) => c.id)).toEqual([login.id, fleet.id, both.id])
    expect(agent.id).not.toBe(both.id)
  })
})
