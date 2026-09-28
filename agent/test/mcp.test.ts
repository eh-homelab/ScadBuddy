import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import type { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { appFetch, BACKEND, baseDeps, connect, firstText, INGRESS, LOOPBACK, MCP_URL, services, testApp, UNTRUSTED } from './helpers/mcp.js'

// /mcp end to end with the MCP SDK's own Streamable HTTP client (issue #251
// "Checks and tests"; spec §13): transport rules, auth per mode, the approval
// gate, streamed progress. The backend is mocked with msw.

const MODELS = [{ slug: 'keychain', name: 'Keychain', description: '', tags: [], origin: 'mine' }]

const server = setupServer(http.get(`${BACKEND}/api/v1/models`, () => HttpResponse.json(MODELS)))
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const clients: Client[] = []
async function open(...args: Parameters<typeof connect>): Promise<Client> {
  const client = await connect(...args)
  clients.push(client)
  return client
}
afterEach(async () => {
  await Promise.all(clients.splice(0).map((c) => c.close()))
})

const HTTPS = { 'x-forwarded-proto': 'https' }

async function bearerApp(tier: 'read' | 'write' | 'outward' = 'outward') {
  const t = testApp()
  const { token, record } = await t.tokens.mint({ name: 'test', tier })
  return { ...t, token, record, auth: { authorization: `Bearer ${token}` } }
}

describe('/mcp: tools over Streamable HTTP', () => {
  it('lists every registry tool, with readOnlyHint on read tools', async () => {
    const { app, auth } = await bearerApp()
    const client = await open(app, { headers: auth })
    const { tools } = await client.listTools()
    expect(tools.map((t) => t.name).sort()).toEqual(ALL_TOOLS.map((t) => t.name).sort())
    const listModels = tools.find((t) => t.name === 'list_models')
    expect(listModels?.annotations?.readOnlyHint).toBe(true)
    expect(tools.find((t) => t.name === 'print_output')?.annotations?.readOnlyHint).toBe(false)
  })

  it('calls a read tool against the (mocked) backend', async () => {
    const { app, auth } = await bearerApp('read')
    const client = await open(app, { headers: auth })
    const result = await client.callTool({ name: 'list_models', arguments: {} })
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual(MODELS)
  })

  it('surfaces a backend error as a tool error with its detail', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/models/:slug`, () =>
        HttpResponse.json({ detail: 'no model "nope"' }, { status: 404 }),
      ),
    )
    const { app, auth } = await bearerApp('read')
    const client = await open(app, { headers: auth })
    const result = await client.callTool({ name: 'get_model', arguments: { slug: 'nope' } })
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('HTTP 404')
    expect(firstText(result)).toContain('no model "nope"')
  })

  it('streams progress notifications for a render before the call completes', async () => {
    let released = false
    const progress: { progress: number; message?: string }[] = []
    let rendersPolled = 0
    server.use(
      http.get(`${BACKEND}/api/v1/models/keychain/schema`, () =>
        HttpResponse.json({ groups: [], parameters: [{ name: 'text', type: 'string', initial: 'hi' }] }),
      ),
      http.post(`${BACKEND}/api/v1/models/keychain/render`, () =>
        HttpResponse.json({ job_id: 'j1', status_url: '/api/v1/jobs/j1' }, { status: 202 }),
      ),
      // The job stays `running` until the client has SEEN a progress
      // notification: the call can only finish if progress streamed first.
      http.get(`${BACKEND}/api/v1/jobs/j1`, () => {
        rendersPolled++
        return HttpResponse.json({
          id: 'j1',
          slug: 'keychain',
          created_at: '2026-09-27T00:00:00Z',
          status: released ? 'done' : 'running',
          log_tail: [released ? 'Rendering finished' : 'Compiling design'],
          bbox_mm: released ? { min: [0, 0, 0], max: [40, 20, 3], size: [40, 20, 3] } : null,
        })
      }),
    )
    const { app, auth } = await bearerApp('write')
    const client = await open(app, { headers: auth })
    const result = await client.callTool({ name: 'render_model', arguments: { slug: 'keychain', params: { text: 'yo' } } }, undefined, {
      onprogress: (p) => {
        progress.push({ progress: p.progress, message: p.message })
        if (p.message?.startsWith('render running')) released = true
      },
    })
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toMatchObject({ job_id: 'j1', status: 'done', bbox_mm: { size: [40, 20, 3] } })
    expect(progress[0]?.message).toBe('render queued as j1')
    expect(progress.some((p) => p.message === 'render running: Compiling design')).toBe(true)
    expect(rendersPolled).toBeGreaterThanOrEqual(2)
    // Monotonic, as the MCP progress utility requires.
    expect(progress.map((p) => p.progress)).toEqual([...progress.map((p) => p.progress)].sort((a, b) => a - b))
  })

  it('ends a session on DELETE', async () => {
    const { app, auth } = await bearerApp()
    const client = await open(app, { headers: auth })
    const transport = client.transport as unknown as StreamableHTTPClientTransport
    const id = transport.sessionId
    expect(id).toBeTruthy()
    await transport.terminateSession()
    const res = await appFetch(app, { headers: { ...auth, 'mcp-session-id': id!, 'content-type': 'application/json', accept: 'application/json, text/event-stream' } })(MCP_URL, {
      method: 'POST',
      body: JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tools/list' }),
    })
    expect(res.status).toBe(404)
  })
})

describe('/mcp: HTTPS and Origin from the shared allowlist (src/http/origins.ts, spec §8.4)', () => {
  for (const mode of ['bearer', 'disabled', 'oidc'] as const) {
    it(`refuses plain HTTP via the ingress with 403 naming the https URL (${mode})`, async () => {
      const { app } = testApp({ settings: { mode } })
      const res = await appFetch(app, { address: INGRESS })(MCP_URL, { method: 'POST', body: '{}' })
      expect(res.status).toBe(403)
      expect(await res.json()).toMatchObject({ https_url: 'https://scadbuddy.test/mcp' })
      // And the MCP client cannot connect at all.
      await expect(connect(app, { address: INGRESS })).rejects.toMatchObject({ code: 403 })
    })

    it(`ignores X-Forwarded-Proto from a peer that is not a trusted proxy (${mode})`, async () => {
      const { app } = testApp({ settings: { mode } })
      await expect(connect(app, { address: UNTRUSTED, headers: HTTPS })).rejects.toMatchObject({ code: 403 })
    })
  }

  it('accepts HTTPS terminated at a trusted proxy, with and without the public Origin', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' } })
    const noOrigin = await open(app, { address: INGRESS, headers: HTTPS })
    expect((await noOrigin.listTools()).tools.length).toBe(ALL_TOOLS.length)
    const publicOrigin = await open(app, { address: INGRESS, headers: { ...HTTPS, origin: 'https://scadbuddy.test' } })
    expect((await publicOrigin.listTools()).tools.length).toBe(ALL_TOOLS.length)
  })

  it('refuses a DNS-rebinding page whose Origin matches its own Host', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' } })
    // Through the ingress: Host and Origin both name the attacker's domain.
    await expect(
      connect(app, { address: INGRESS, headers: { ...HTTPS, host: 'evil.test', origin: 'https://evil.test' } }),
    ).rejects.toMatchObject({ code: 403 })
    // Straight to the port from this machine: loopback peer, rebound name.
    await expect(
      connect(app, { address: LOOPBACK, headers: { host: 'evil.test:8081', origin: 'http://evil.test:8081' } }),
    ).rejects.toMatchObject({ code: 403 })
    // A foreign Origin on the right Host.
    await expect(
      connect(app, { address: INGRESS, headers: { ...HTTPS, origin: 'https://evil.example' } }),
    ).rejects.toMatchObject({ code: 403 })
  })

  it('keeps the loopback development exception: a local browser on localhost', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' } })
    const client = await open(app, {
      address: LOOPBACK,
      headers: { host: 'localhost:8081', origin: 'http://localhost:8081' },
    })
    expect((await client.listTools()).tools.length).toBe(ALL_TOOLS.length)
  })
})

describe('/mcp: bearer mode (default)', () => {
  it('401s a request with no token, with WWW-Authenticate: Bearer', async () => {
    const { app } = testApp()
    const res = await appFetch(app)(MCP_URL, { method: 'POST', body: '{}' })
    expect(res.status).toBe(401)
    expect(res.headers.get('www-authenticate')).toMatch(/^Bearer /)
    await expect(connect(app)).rejects.toThrow()
  })

  it('401s an unknown token', async () => {
    const { app } = testApp()
    const res = await appFetch(app, { headers: { authorization: 'Bearer sbmcp_nope' } })(MCP_URL, {
      method: 'POST',
      body: '{}',
    })
    expect(res.status).toBe(401)
    expect(res.headers.get('www-authenticate')).toContain('invalid_token')
  })

  it('401s an expired token', async () => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'old', tier: 'read', expiresAt: new Date(Date.now() - 1000) })
    await expect(connect(t.app, { headers: { authorization: `Bearer ${token}` } })).rejects.toThrow()
  })

  it('stops a revoked token at once, mid-session', async () => {
    const { app, tokens, record, auth } = await bearerApp('read')
    const client = await open(app, { headers: auth })
    expect((await client.callTool({ name: 'list_models', arguments: {} })).isError).toBeFalsy()
    await tokens.revoke(record.id)
    await expect(client.callTool({ name: 'list_models', arguments: {} })).rejects.toThrow()
  })

  it('refuses a tool above the token tier', async () => {
    const { app, auth } = await bearerApp('read')
    const client = await open(app, { headers: auth })
    const result = await client.callTool({ name: 'install_font', arguments: { family: 'Lobster Two' } })
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('needs the "write" tier')
  })

  it('does not let another principal use a session', async () => {
    const t = testApp()
    const a = await t.tokens.mint({ name: 'a', tier: 'outward' })
    const b = await t.tokens.mint({ name: 'b', tier: 'outward' })
    const client = await open(t.app, { headers: { authorization: `Bearer ${a.token}` } })
    const id = (client.transport as { sessionId?: string }).sessionId!
    const res = await appFetch(t.app, {
      headers: {
        authorization: `Bearer ${b.token}`,
        'mcp-session-id': id,
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
      },
    })(MCP_URL, { method: 'POST', body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }) })
    expect(res.status).toBe(403)
  })
})

describe('/mcp: disabled mode', () => {
  it('runs as anonymous with full access by default, still behind the approval gate', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' } })
    const client = await open(app)
    expect((await client.callTool({ name: 'list_models', arguments: {} })).isError).toBeFalsy()
    const result = await client.callTool({ name: 'delete_model', arguments: { slug: 'keychain' } })
    expect(result.isError).toBeFalsy()
    // Full access reaches the gate; the gate, not auth, stops the delete.
    expect(firstText(result)).toMatchObject({ status: 'pending_approval' })
  })

  it('keeps concurrent anonymous sessions apart: the session id is the capability', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' } })
    const a = await open(app)
    const b = await open(app)
    const idA = (a.transport as unknown as StreamableHTTPClientTransport).sessionId!
    const idB = (b.transport as unknown as StreamableHTTPClientTransport).sessionId!
    // 256 CSPRNG bits, base64url: 43 characters, and distinct.
    expect(idA).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(idB).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(idA).not.toBe(idB)

    const { pending_action_id } = firstText(
      await a.callTool({ name: 'delete_model', arguments: { slug: 'keychain' } }),
    ) as { pending_action_id: string }
    await b.callTool({ name: 'send_to_bambuddy', arguments: { output_id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' } })

    const listA = firstText(await a.callTool({ name: 'list_pending_actions', arguments: {} })) as { tool: string }[]
    const listB = firstText(await b.callTool({ name: 'list_pending_actions', arguments: {} })) as { tool: string }[]
    expect(listA.map((x) => x.tool)).toEqual(['delete_model'])
    expect(listB.map((x) => x.tool)).toEqual(['send_to_bambuddy'])

    const crossConfirm = await b.callTool({ name: 'confirm_action', arguments: { pending_action_id } })
    expect(firstText(crossConfirm)).toContain('no pending action')
  })

  it('enforces a lowered anonymous cap', async () => {
    const { app } = testApp({ settings: { mode: 'disabled', anonymousCap: 'read' } })
    const client = await open(app)
    expect((await client.callTool({ name: 'list_models', arguments: {} })).isError).toBeFalsy()
    const write = await client.callTool({ name: 'install_font', arguments: { family: 'Lobster Two' } })
    expect(write.isError).toBe(true)
    expect(firstText(write)).toContain('needs the "write" tier')
  })
})

describe('/mcp: oidc mode', () => {
  it('is not implemented yet and says so with a 501', async () => {
    const { app } = testApp({ settings: { mode: 'oidc' } })
    const res = await appFetch(app)(MCP_URL, { method: 'POST', body: '{}' })
    expect(res.status).toBe(501)
    expect(await res.text()).toContain('#262')
  })
})

// With ai_approvals behind it, confirm runs an approved call: test/confirm.pg.test.ts.
describe('/mcp: outward tools prepare, and with no approval store confirm is refused (#258)', () => {
  it('prepares a pending action instead of printing, and refuses to confirm it', async () => {
    // No backend handler for the print routes: an attempt to call them would
    // fail the test through onUnhandledRequest: 'error'.
    const { app, auth } = await bearerApp('outward')
    const client = await open(app, { headers: auth })
    const prepared = await client.callTool({
      name: 'print_output',
      arguments: { output_id: '0123456789abcdef0123456789abcdef', nozzles: [{ size: '0.4' }], tier: 'fine', copies: 2 },
    })
    expect(prepared.isError).toBeFalsy()
    const body = firstText(prepared) as { status: string; pending_action_id: string; summary: string }
    expect(body.status).toBe('pending_approval')
    expect(body.summary).toBe('Print output 0123456789abcdef0123456789abcdef: 2 copies of plate 1 with a 0.4 mm nozzle, fine quality (other choices as the print dialog opens)')

    const listed = firstText(await client.callTool({ name: 'list_pending_actions', arguments: {} }))
    expect(listed).toEqual([expect.objectContaining({ pending_action_id: body.pending_action_id, tool: 'print_output' })])

    const confirmed = await client.callTool({
      name: 'confirm_action',
      arguments: { pending_action_id: body.pending_action_id },
    })
    expect(confirmed.isError).toBe(true)
    expect(firstText(confirmed)).toContain('#258')
    expect(firstText(confirmed)).toContain('Nothing was sent')
  })

  it("does not reveal or confirm another caller's pending action", async () => {
    const t = testApp()
    const a = await t.tokens.mint({ name: 'a', tier: 'outward' })
    const b = await t.tokens.mint({ name: 'b', tier: 'outward' })
    const ca = await open(t.app, { headers: { authorization: `Bearer ${a.token}` } })
    const cb = await open(t.app, { headers: { authorization: `Bearer ${b.token}` } })
    const { pending_action_id } = firstText(
      await ca.callTool({ name: 'send_to_bambuddy', arguments: { output_id: '0123456789abcdef0123456789abcdef' } }),
    ) as { pending_action_id: string }
    const res = await cb.callTool({ name: 'confirm_action', arguments: { pending_action_id } })
    expect(res.isError).toBe(true)
    expect(firstText(res)).toContain('no pending action')
    expect(firstText(await cb.callTool({ name: 'list_pending_actions', arguments: {} }))).toEqual([])
  })

  it('a write token cannot even prepare an outward action', async () => {
    const { app, auth } = await bearerApp('write')
    const client = await open(app, { headers: auth })
    const res = await client.callTool({ name: 'send_to_bambuddy', arguments: { output_id: '0123456789abcdef0123456789abcdef' } })
    expect(res.isError).toBe(true)
    expect(firstText(res)).toContain('needs the "outward" tier')
  })
})

describe('/mcp: fail closed', () => {
  it('answers 503 "AI disabled: no database" when no database is configured (spec §9)', async () => {
    const app = createApp(
      baseDeps({
        database: undefined,
        credentials: undefined,
        mcp: {
          tools: ALL_TOOLS,
          services: services(),
          tokens: testApp().tokens,
          authSettings: () => ({ mode: 'disabled', anonymousCap: 'outward' }),
        },
      }),
    )
    const res = await appFetch(app)(MCP_URL, { method: 'POST', body: '{}' })
    expect(res.status).toBe(503)
    expect(await res.text()).toContain('AI disabled: no database')
    await expect(connect(app)).rejects.toMatchObject({ code: 503 })
  })

  it("answers 503 until the database's migrations have applied (db.ts ready())", async () => {
    let migrated = false
    const { app } = testApp({
      settings: { mode: 'disabled' },
      deps: { database: { ping: async () => true, ready: async () => migrated } },
    })
    const res = await appFetch(app)(MCP_URL, { method: 'POST', body: '{}' })
    expect(res.status).toBe(503)
    expect(await res.text()).toContain('migrations')
    migrated = true
    const client = await open(app)
    expect((await client.listTools()).tools.length).toBe(ALL_TOOLS.length)
  })

  it('falls back to bearer with no valid tokens when the auth settings cannot be read', async () => {
    const tokens = testApp().tokens
    const { token } = await tokens.mint({ name: 'valid', tier: 'outward' })
    const { app } = testApp({
      tokens,
      authSettings: () => {
        throw new Error('settings table unreachable')
      },
    })
    // Not `disabled`, whatever the operator had configured: no anonymous access …
    const anon = await appFetch(app)(MCP_URL, { method: 'POST', body: '{}' })
    expect(anon.status).toBe(401)
    expect(anon.headers.get('www-authenticate')).toMatch(/^Bearer /)
    // … and not even a token that is valid in the configured store.
    await expect(connect(app, { headers: { authorization: `Bearer ${token}` } })).rejects.toMatchObject({ code: 401 })
  })
})
