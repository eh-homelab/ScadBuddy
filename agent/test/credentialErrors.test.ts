import { afterEach, describe, expect, it } from 'vitest'
import {
  CLEARED_COOLDOWN_MS,
  classifyFailure,
  cooldownUntil,
  DEFAULT_COOLDOWN_MS,
  describeApiFailure,
  type FailureClass,
  type FailureEvidence,
  MAX_COOLDOWN_MS,
  PROBE_FALLBACK_MODEL,
  probeCredential,
  rateLimitResetFromHeaders,
  refusesTheKey,
} from '../src/harness/credentialErrors.js'
import { type FakeAnthropic, startFakeAnthropic } from './support/fakeAnthropic.js'

describe('classifyFailure (#1093)', () => {
  // What Claude Code 2.1.283 reports for each response, measured against the
  // fake endpoint (test/fallback.e2e.test.ts covers the same end to end).
  const cases: [string, FailureEvidence, FailureClass][] = [
    ['401 retried', { status: 401, category: 'authentication_failed' }, 'permanent'],
    ['403 (Claude Code says authentication_failed)', { status: 403, category: 'authentication_failed' }, 'permanent'],
    ['403 from a gateway, uncategorised', { status: 403, category: 'unknown' }, 'permanent'],
    ['402', { status: 402 }, 'permanent'],
    ['billing', { status: 400, category: 'billing_error', message: 'Credit balance is too low' }, 'permanent'],
    ['account on hold', { category: 'account_on_hold' }, 'permanent'],
    ['a 400 that is a billing message', { status: 400, category: 'unknown', message: 'Your credit balance is too low to access the Anthropic API.' }, 'permanent'],
    ['429', { status: 429, category: 'rate_limit' }, 'rate_limited'],
    ['429 uncategorised', { status: 429, category: 'unknown' }, 'rate_limited'],
    ['529', { status: 529, category: 'overloaded' }, 'transient'],
    ['529 as Claude Code gives up', { status: 529, category: 'server_error' }, 'transient'],
    ['500', { status: 500, category: 'unknown' }, 'transient'],
    ['503 uncategorised', { status: 503 }, 'transient'],
    ['connection error', { status: null, category: 'unknown' }, 'transient'],
    ['timeout', { status: 408 }, 'transient'],
    ['400 bad request', { status: 400, category: 'unknown', message: 'API Error: 400 messages: bad' }, 'other'],
    ['invalid request', { status: 400, category: 'invalid_request', message: 'prompt is too long' }, 'other'],
    ['model not found', { status: 404, category: 'model_not_found' }, 'other'],
    ['404 uncategorised', { status: 404 }, 'other'],
    ['max output tokens', { category: 'max_output_tokens' }, 'other'],
    ['nothing at all', {}, 'other'],
  ]
  it.each(cases)('%s', (_name, evidence, expected) => {
    expect(classifyFailure(evidence)).toBe(expected)
  })
})

describe('describeApiFailure (#1101)', () => {
  it.each<[FailureEvidence, string]>([
    [
      { status: 403, message: 'API Error: 403 forbidden' },
      'the Claude credential was rejected (HTTP 403); check it under Settings → AI: API Error: 403 forbidden',
    ],
    [
      { status: 400, message: 'Your credit balance is too low' },
      'the Claude credential was rejected (HTTP 400); check it under Settings → AI: Your credit balance is too low',
    ],
    // The category decides over a status that would say otherwise.
    [
      { status: 400, category: 'billing_error', message: 'API Error' },
      'the Claude credential was rejected (HTTP 400); check it under Settings → AI: API Error',
    ],
    [{ status: 429, message: 'slow down' }, 'the Claude credential is rate limited (HTTP 429); try again later: slow down'],
    [{ status: 529, message: 'Overloaded' }, 'the model endpoint failed (HTTP 529); try again: Overloaded'],
    [{ status: null, message: 'Connection error.' }, 'the model endpoint failed (no response); try again: Connection error.'],
    [{ status: 400, message: 'bad request' }, 'the model API refused the request (HTTP 400): bad request'],
    [{ status: 400, message: '  ' }, 'the model API refused the request (HTTP 400)'],
  ])('%j', (evidence, said) => {
    expect(describeApiFailure(evidence)).toBe(said)
  })

  it('says the credential works when a probe found it does, whatever the status says', () => {
    expect(describeApiFailure({ status: 403, message: 'blocked by policy' }, { credentialWorks: true })).toBe(
      'the model endpoint refused this request (HTTP 403); the credential itself works: blocked by policy',
    )
  })
})

describe('rateLimitResetFromHeaders', () => {
  const now = Date.parse('2026-10-03T12:00:00Z')
  const h = (init: Record<string, string>) => new Headers(init)

  it('takes retry-after-ms, then retry-after in seconds or as a date', () => {
    expect(rateLimitResetFromHeaders(h({ 'retry-after-ms': '1500', 'retry-after': '90' }), now)).toBe(now + 1500)
    expect(rateLimitResetFromHeaders(h({ 'retry-after': '90' }), now)).toBe(now + 90_000)
    expect(rateLimitResetFromHeaders(h({ 'retry-after': '0.5' }), now)).toBe(now + 500)
    expect(rateLimitResetFromHeaders(h({ 'retry-after': 'Sat, 03 Oct 2026 12:05:00 GMT' }), now)).toBe(now + 300_000)
  })

  it('prefers retry-after to the bucket resets', () => {
    expect(
      rateLimitResetFromHeaders(
        h({ 'retry-after': '20', 'anthropic-ratelimit-requests-remaining': '0', 'anthropic-ratelimit-requests-reset': '2026-10-03T12:10:00Z' }),
        now,
      ),
    ).toBe(now + 20_000)
  })

  it('takes the latest reset of an exhausted bucket', () => {
    const headers = h({
      'anthropic-ratelimit-requests-remaining': '0',
      'anthropic-ratelimit-requests-reset': '2026-10-03T12:00:30Z',
      'anthropic-ratelimit-input-tokens-remaining': '0',
      'anthropic-ratelimit-input-tokens-reset': '2026-10-03T12:00:45Z',
      'anthropic-ratelimit-output-tokens-remaining': '9000',
      'anthropic-ratelimit-output-tokens-reset': '2026-10-03T12:00:59Z',
    })
    expect(rateLimitResetFromHeaders(headers, now)).toBe(Date.parse('2026-10-03T12:00:45Z'))
  })

  it('takes the latest reset of any bucket when none is exhausted (a request too big for what is left)', () => {
    const headers = h({
      'anthropic-ratelimit-tokens-remaining': '120',
      'anthropic-ratelimit-tokens-reset': '2026-10-03T12:00:20Z',
      'anthropic-ratelimit-requests-remaining': '40',
      'anthropic-ratelimit-requests-reset': '2026-10-03T12:00:05Z',
    })
    expect(rateLimitResetFromHeaders(headers, now)).toBe(Date.parse('2026-10-03T12:00:20Z'))
  })

  it('takes the unified reset (epoch seconds) of a subscription token', () => {
    expect(rateLimitResetFromHeaders(h({ 'anthropic-ratelimit-unified-reset': String(now / 1000 + 3600) }), now)).toBe(
      now + 3_600_000,
    )
  })

  it('names no time when the headers do not, or are malformed', () => {
    expect(rateLimitResetFromHeaders(h({}), now)).toBeUndefined()
    expect(rateLimitResetFromHeaders(h({ 'retry-after': 'soon', 'anthropic-ratelimit-requests-reset': 'later' }), now)).toBeUndefined()
  })
})

describe('cooldownUntil', () => {
  const now = 1_000_000
  it('defaults, and is held between a second and a day from now', () => {
    expect(cooldownUntil(undefined, now).getTime()).toBe(now + DEFAULT_COOLDOWN_MS)
    expect(cooldownUntil(now - 5000, now).getTime()).toBe(now + 1000)
    expect(cooldownUntil(now + 10 * MAX_COOLDOWN_MS, now).getTime()).toBe(now + MAX_COOLDOWN_MS)
    expect(cooldownUntil(now + 42_000, now).getTime()).toBe(now + 42_000)
  })
})

describe('probeCredential', () => {
  let fake: FakeAnthropic | undefined
  afterEach(async () => {
    await fake?.close()
    fake = undefined
  })
  const now = () => Date.parse('2026-10-03T12:00:00Z')

  it('asks the gateway once with the credential and reads the reset from its 429', async () => {
    fake = await startFakeAnthropic(() => ({
      error: {
        status: 429,
        type: 'rate_limit_error',
        message: 'slow down',
        headers: {
          'anthropic-ratelimit-requests-remaining': '0',
          'anthropic-ratelimit-requests-reset': '2026-10-03T12:03:00Z',
        },
      },
    }))
    const until = await probeCredential({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-probe-token' }, { model: 'claude-sonnet-4-5', now })
    expect(until).toEqual({ verdict: 'rate_limited', until: new Date('2026-10-03T12:03:00.000Z') })
    const [call] = fake.messageCalls()
    expect(fake.messageCalls()).toHaveLength(1)
    expect(call?.headers.authorization).toBe('Bearer gw-probe-token')
    expect(call?.body).toMatchObject({ model: 'claude-sonnet-4-5', max_tokens: 1 })
  })

  it('sends an API key as x-api-key to Anthropic, with a model when the query reported none', async () => {
    const seen: { url: string; headers: Record<string, string>; body: string }[] = []
    const fetchStub: typeof fetch = (input, init) => {
      seen.push({ url: String(input), headers: init?.headers as Record<string, string>, body: String(init?.body) })
      return Promise.resolve(new Response('{}', { status: 429, headers: { 'retry-after': '30' } }))
    }
    const until = await probeCredential({ kind: 'anthropic_api_key', secret: 'sk-ant-probe' }, { model: undefined, fetch: fetchStub, now })
    expect(until).toEqual({ verdict: 'rate_limited', until: new Date(now() + 30_000) })
    expect(seen[0]?.url).toBe('https://api.anthropic.com/v1/messages')
    expect(seen[0]?.headers['x-api-key']).toBe('sk-ant-probe')
    expect(seen[0]?.headers.authorization).toBeUndefined()
    expect(JSON.parse(seen[0]!.body)).toMatchObject({ model: PROBE_FALLBACK_MODEL })
  })

  it('never asks about an OAuth token: unknown, so a 429 or refusal cools down and never disables it', async () => {
    // How Claude Code presents the token to the Messages API is not measured here; sent as
    // x-api-key it is a 401, which would read as the token's own refusal.
    let calls = 0
    const counting: typeof fetch = () => {
      calls += 1
      return Promise.resolve(new Response('{}', { status: 401 }))
    }
    expect(
      await probeCredential({ kind: 'claude_oauth_token', secret: 'sk-ant-oat01-probe' }, { model: undefined, fetch: counting, now }),
    ).toEqual({ verdict: 'unknown', until: new Date(now() + DEFAULT_COOLDOWN_MS) })
    expect(calls).toBe(0)
  })

  it('falls back to the default cooldown when the endpoint cannot be asked or names no time', async () => {
    const failing: typeof fetch = () => Promise.reject(new Error('connection refused'))
    expect(await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: failing, now })).toEqual({
      verdict: 'unknown',
      until: new Date(now() + DEFAULT_COOLDOWN_MS),
    })
    const bare: typeof fetch = () => Promise.resolve(new Response('{}', { status: 429 }))
    expect(await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: bare, now })).toEqual({
      verdict: 'rate_limited',
      until: new Date(now() + DEFAULT_COOLDOWN_MS),
    })
    const broken: typeof fetch = () => Promise.resolve(new Response('{}', { status: 500, headers: { 'retry-after': '900' } }))
    expect(await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: broken, now })).toEqual({
      verdict: 'unknown',
      until: new Date(now() + DEFAULT_COOLDOWN_MS),
    })
  })

  it('calls an answered probe usable again in a second, whatever its reset headers say', async () => {
    const ok: typeof fetch = () =>
      Promise.resolve(
        new Response('{}', {
          status: 200,
          headers: {
            'anthropic-ratelimit-tokens-remaining': '100',
            'anthropic-ratelimit-tokens-reset': '2026-10-03T13:00:00Z',
            'anthropic-ratelimit-unified-reset': String(now() / 1000 + 86_000),
          },
        }),
      )
    expect(await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: ok, now })).toEqual({
      verdict: 'answered',
      until: new Date(now() + CLEARED_COOLDOWN_MS),
    })
  })

  it('reports a probe refused with a billing 400 as refused, and any other 400 as unknown', async () => {
    const answer = (message: string): typeof fetch => () =>
      Promise.resolve(new Response(JSON.stringify({ type: 'error', error: { type: 'invalid_request_error', message } }), { status: 400 }))
    const billing = await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: answer('Your credit balance is too low'), now })
    expect(billing.verdict).toBe('refused')
    const other = await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: answer('model: unknown'), now })
    expect(other).toEqual({ verdict: 'unknown', until: new Date(now() + DEFAULT_COOLDOWN_MS) })
  })

  it.each([
    [401, 'invalid x-api-key', true],
    [402, '', true],
    [403, 'Your API key does not have permission to use the API', true],
    [403, 'This organization has been disabled.', true],
    [403, 'Your credit balance is too low', true],
    [400, 'Your credit balance is too low', true],
    [403, 'Request blocked by policy', false],
    [403, 'Your organization does not have access to model claude-opus-4-1', false],
    [403, '', false],
    [400, 'messages: bad', false],
  ] as const)('refusesTheKey(%i, %j) is %s', (status, body, expected) => {
    expect(refusesTheKey(status, body)).toBe(expected)
  })

  it('treats a probe answered with a 403 that is not about the key as unknown', async () => {
    const blocked: typeof fetch = () => Promise.resolve(new Response('blocked by policy', { status: 403 }))
    expect(await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: blocked, now })).toEqual({
      verdict: 'unknown',
      until: new Date(now() + DEFAULT_COOLDOWN_MS),
    })
  })

  it('stops a probe when the caller aborts', async () => {
    const stop = new AbortController()
    const hanging: typeof fetch = (_input, init) =>
      new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))
    const started = Date.now()
    const verdict = probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: hanging, now, signal: stop.signal })
    stop.abort()
    expect((await verdict).verdict).toBe('unknown')
    expect(Date.now() - started).toBeLessThan(1000)
  })

  it.each([401, 402, 403])('reports a probe answered %i as refused outright', async (status) => {
    const refused: typeof fetch = () =>
      Promise.resolve(
        new Response(JSON.stringify({ type: 'error', error: { type: 'authentication_error', message: 'invalid api key' } }), { status }),
      )
    const verdict = await probeCredential({ kind: 'anthropic_api_key', secret: 'k' }, { model: undefined, fetch: refused, now })
    expect(verdict.verdict).toBe('refused')
    expect('reason' in verdict && verdict.reason).toMatch(`the probe was refused (HTTP ${status}): `)
    expect('reason' in verdict && verdict.reason).toContain('authentication_error')
  })

  it('does not probe a gateway host the egress rules refuse', async () => {
    let called = false
    const fetchStub: typeof fetch = () => {
      called = true
      return Promise.resolve(new Response('{}'))
    }
    const until = await probeCredential(
      { kind: 'gateway', baseUrl: 'http://metadata.example', secret: 'gw' },
      { model: undefined, fetch: fetchStub, now, resolveHost: () => Promise.resolve(['169.254.169.254']) },
    )
    expect(called).toBe(false)
    expect(until).toEqual({ verdict: 'unknown', until: new Date(now() + DEFAULT_COOLDOWN_MS) })
  })
})
