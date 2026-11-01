import { createServer, request, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CredentialGateway } from '../src/gateway/server.js'
import { newToken, sameToken } from '../src/gateway/tokens.js'
import { type FakeAnthropic, type Reply, REPLY_COST_USD, STALL, startFakeAnthropic } from './support/fakeAnthropic.js'

// The credential gateway (src/gateway/server.ts, plan 2026-10-13 Step 9.4): what Claude Code's
// token may reach, and what the endpoint sees. The harness's own use of it, with the real
// binary and each credential kind, is test/run.test.ts; its spend in a session's cost_usd,
// test/sessionAffinity.temporal.test.ts.

const API_KEY = 'sk-ant-api03-gateway-test-key-0000'
const GATEWAY_SECRET = 'gw-gateway-test-secret-1111'

/** A POST whose path is sent exactly as written (fetch and URL would remove dot segments). */
function rawPost(baseUrl: string, path: string, headers: Record<string, string>): Promise<{ status: number; body: string }> {
  const url = new URL(baseUrl)
  const body = messages(true)
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: url.hostname,
        port: url.port,
        method: 'POST',
        path: `${url.pathname}${path}`,
        headers: { ...headers, 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) },
      },
      (res) => {
        let text = ''
        res.setEncoding('utf8')
        res.on('data', (c: string) => (text += c))
        res.on('end', () => resolve({ status: res.statusCode ?? 0, body: text }))
      },
    )
    req.on('error', reject)
    req.end(body)
  })
}

const messages = (stream: boolean) =>
  JSON.stringify({ model: 'claude-sonnet-4-5', max_tokens: 5, stream, messages: [{ role: 'user', content: 'hi' }] })

describe('the credential gateway', () => {
  let fake: FakeAnthropic
  let reply: Reply
  let gateway: CredentialGateway

  beforeEach(async () => {
    reply = { text: 'ok' }
    fake = await startFakeAnthropic(() => reply)
    gateway = await CredentialGateway.start({ anthropicUrl: fake.url })
  })
  afterEach(async () => {
    await gateway.close()
    await fake.close()
  })

  const post = (url: string, headers: Record<string, string>, body = messages(true)) =>
    fetch(`${url}/v1/messages?beta=true`, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body })

  it('adds the API key upstream in place of the token, streams the reply through and prices it', async () => {
    const spent: number[] = []
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY }, { onSpend: (usd) => spent.push(usd) })
    const res = await post(lease.baseUrl, { 'x-api-key': lease.token, 'anthropic-beta': 'x-test' })
    expect(res.status).toBe(200)
    expect(res.headers.get('content-type')).toContain('text/event-stream')
    const text = await res.text()
    expect(text).toContain('message_stop')
    const [call] = fake.messageCalls()
    expect(call?.path).toBe('/v1/messages')
    expect(call?.headers['x-api-key']).toBe(API_KEY)
    expect(call?.headers.authorization).toBeUndefined()
    expect(call?.headers['anthropic-beta']).toBe('x-test')
    expect(JSON.stringify(call?.headers)).not.toContain(lease.token)
    expect(spent).toHaveLength(1)
    expect(spent[0]).toBeCloseTo(REPLY_COST_USD, 12)
  })

  it("sends a gateway kind's requests to its own base URL with its token as a bearer, and prices a JSON reply", async () => {
    const spent: number[] = []
    const lease = gateway.lease({ kind: 'gateway', baseUrl: `${fake.url}/`, secret: GATEWAY_SECRET }, { onSpend: (usd) => spent.push(usd) })
    const res = await post(lease.baseUrl, { authorization: `Bearer ${lease.token}` }, messages(false))
    expect(res.status).toBe(200)
    expect(((await res.json()) as { type: string }).type).toBe('message')
    expect(fake.messageCalls()[0]?.headers.authorization).toBe(`Bearer ${GATEWAY_SECRET}`)
    expect(spent[0]).toBeCloseTo(REPLY_COST_USD, 12)
  })

  it('sends an OAuth token as a bearer, keeping the beta header Claude Code sends with one', async () => {
    const lease = gateway.lease({ kind: 'claude_oauth_token', secret: 'sk-ant-oat01-gateway-test' })
    const res = await post(lease.baseUrl, { authorization: `Bearer ${lease.token}`, 'anthropic-beta': 'oauth-2025-04-20' })
    await res.text()
    const [call] = fake.messageCalls()
    expect(call?.headers.authorization).toBe('Bearer sk-ant-oat01-gateway-test')
    expect(call?.headers['anthropic-beta']).toBe('oauth-2025-04-20')
  })

  it('refuses a wrong token, a token in only one of path and header, and a released one, and sends none of them on', async () => {
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY })
    const other = gateway.lease({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-other' })
    const base = lease.baseUrl.slice(0, lease.baseUrl.indexOf('/t/'))
    const cases: [string, Record<string, string>][] = [
      [`${base}/t/${newToken()}`, { 'x-api-key': lease.token }],
      [lease.baseUrl, { 'x-api-key': other.token }],
      [lease.baseUrl, {}],
      [lease.baseUrl, { 'x-api-key': `${lease.token}x` }],
      [`${base}`, { 'x-api-key': lease.token }],
    ]
    for (const [url, headers] of cases) {
      const res = await post(url, headers)
      expect(res.status).toBe(401)
      expect(await res.json()).toMatchObject({ type: 'error', error: { type: 'authentication_error' } })
    }
    lease.release()
    expect((await post(lease.baseUrl, { 'x-api-key': lease.token })).status).toBe(401)
    expect(fake.messageCalls()).toHaveLength(0)
    expect(gateway.leases).toBe(1)
  })

  it.each([
    '/v1/./messages',
    '/v1//messages',
    '/v1/%2e/messages',
    '/v1/%2E%2E/v1/messages',
    '/v1/messages/',
    '/v1/messages%2f',
    '/v1/x/../messages',
    '/v1/messages/../messages',
    '/v1/messages;x',
    '//v1/messages',
  ])('refuses %s, a path the endpoint might read as another, and sends nothing on', async (path) => {
    const spent: number[] = []
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY }, { onSpend: (usd) => spent.push(usd) })
    // A raw request: fetch would normalise the dot segments itself.
    const { status, body } = await rawPost(lease.baseUrl, path, { 'x-api-key': lease.token })
    expect(status).toBe(403)
    expect(JSON.parse(body)).toMatchObject({ type: 'error', error: { type: 'permission_error' } })
    expect(fake.requests).toHaveLength(0)
    expect(spent).toEqual([])
  })

  it.each([
    ['POST', '/v1/files'],
    ['POST', '/v1/messages/batches'],
    ['GET', '/v1/messages'],
    ['GET', '/v1/organizations/me'],
    ['DELETE', '/v1/models'],
  ])('refuses %s %s, an endpoint Claude Code does not call', async (method, path) => {
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY })
    const res = await fetch(`${lease.baseUrl}${path}`, {
      method,
      headers: { 'x-api-key': lease.token, 'content-type': 'application/json' },
      ...(method === 'POST' ? { body: '{}' } : {}),
    })
    expect(res.status).toBe(403)
    expect(fake.requests).toHaveLength(0)
  })

  it('passes token counting and the model list, and prices neither', async () => {
    const spent: number[] = []
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY }, { onSpend: (usd) => spent.push(usd) })
    const count = await fetch(`${lease.baseUrl}/v1/messages/count_tokens?beta=true`, {
      method: 'POST',
      headers: { 'x-api-key': lease.token, 'content-type': 'application/json' },
      body: messages(false),
    })
    expect(await count.json()).toEqual({ input_tokens: 10 })
    const models = await fetch(`${lease.baseUrl}/v1/models`, { headers: { 'x-api-key': lease.token } })
    expect(models.status).toBe(200)
    expect(fake.requests.map((r) => `${r.method} ${r.path}`)).toEqual(['POST /v1/messages/count_tokens', 'GET /v1/models'])
    expect(spent).toEqual([])
  })

  it("keeps a gateway kind's base path, and sends nothing outside it", async () => {
    const lease = gateway.lease({ kind: 'gateway', baseUrl: `${fake.url}/anthropic`, secret: GATEWAY_SECRET })
    const res = await post(lease.baseUrl, { authorization: `Bearer ${lease.token}` })
    await res.text()
    expect(fake.requests.map((r) => r.path)).toEqual(['/anthropic/v1/messages'])
  })

  it('passes a refusal through with its status and headers, and prices nothing', async () => {
    reply = { error: { status: 429, type: 'rate_limit_error', message: 'slow down', headers: { 'retry-after': '7' } } }
    const spent: number[] = []
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY }, { onSpend: (usd) => spent.push(usd) })
    const res = await post(lease.baseUrl, { 'x-api-key': lease.token })
    expect(res.status).toBe(429)
    expect(res.headers.get('retry-after')).toBe('7')
    expect(await res.json()).toMatchObject({ error: { type: 'rate_limit_error' } })
    expect(spent).toEqual([])
  })

  it('prices a stream cut off before its end at what it used', async () => {
    reply = STALL
    const spent: number[] = []
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY }, { onSpend: (usd) => spent.push(usd) })
    const stop = new AbortController()
    const res = await fetch(`${lease.baseUrl}/v1/messages`, {
      method: 'POST',
      headers: { 'x-api-key': lease.token, 'content-type': 'application/json' },
      body: messages(true),
      signal: stop.signal,
    })
    const reader = res.body!.getReader()
    let seen = ''
    while (!seen.includes('content_block_delta')) seen += new TextDecoder().decode((await reader.read()).value)
    stop.abort()
    await expect.poll(() => spent.length).toBe(1)
    // message_start's 100k input tokens at claude-sonnet-4-5's $3 per MTok, and the one output token it reported.
    expect(spent[0]).toBeCloseTo((100_000 * 3 + 15) / 1_000_000, 12)
  })

  describe('a gateway kind’s upstream', () => {
    let redirector: Server
    let redirectorUrl: string
    beforeEach(async () => {
      redirector = createServer((_req, res) => {
        res.writeHead(307, { location: `${fake.url}/v1/messages` })
        res.end()
      })
      await new Promise<void>((resolve) => redirector.listen(0, '127.0.0.1', resolve))
      redirectorUrl = `http://127.0.0.1:${(redirector.address() as AddressInfo).port}`
    })
    afterEach(async () => {
      await new Promise<void>((resolve) => redirector.close(() => resolve()))
    })

    it('is never followed through a redirect', async () => {
      const lease = gateway.lease({ kind: 'gateway', baseUrl: redirectorUrl, secret: GATEWAY_SECRET })
      const res = await post(lease.baseUrl, { authorization: `Bearer ${lease.token}` })
      expect(res.status).toBe(502)
      expect(fake.messageCalls()).toHaveLength(0)
    })

    it('is checked as at save time: a metadata address is refused before anything is sent', async () => {
      const seen: string[] = []
      const checked = await CredentialGateway.start({ resolve: async () => ['169.254.169.254'], fetch: async (input) => (seen.push(String(input)), new Response('')) })
      try {
        const lease = checked.lease({ kind: 'gateway', baseUrl: 'http://metadata.example', secret: GATEWAY_SECRET })
        const res = await post(lease.baseUrl, { authorization: `Bearer ${lease.token}` })
        expect(res.status).toBe(502)
        expect(JSON.stringify(await res.json())).not.toContain(GATEWAY_SECRET)
        expect(seen).toEqual([])
      } finally {
        await checked.close()
      }
    })
  })

  it('listens on loopback only', () => {
    const lease = gateway.lease({ kind: 'anthropic_api_key', secret: API_KEY })
    expect(new URL(lease.baseUrl).hostname).toBe('127.0.0.1')
    expect(lease.baseUrl).not.toContain(API_KEY)
  })
})

describe('gateway tokens', () => {
  it('are random, URL-safe and compared whole', () => {
    const a = newToken()
    expect(a).toMatch(/^[A-Za-z0-9_-]{32}$/)
    expect(newToken()).not.toBe(a)
    expect(sameToken(a, a)).toBe(true)
    expect(sameToken(a.slice(0, -1), a)).toBe(false)
    expect(sameToken(`${a}x`, a)).toBe(false)
  })
})
