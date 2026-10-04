import type { SDKAssistantMessageError } from '@anthropic-ai/claude-agent-sdk'
import type { Credential } from '../credentials.js'
import { assertGatewayHostAllowed, type Resolver, systemResolver } from '../http/egress.js'

// Why a query failed, per credential (#1093). Claude Code reports a failed
// model request three ways, all measured against test/support/fakeAnthropic.ts
// on Claude Code 2.1.283 (test/fallback.e2e.test.ts):
//
//   - `system/api_retry` before each retry it makes, with `error_status` (null
//     for a connection error) and an `error` category. It retries 401, 429
//     without a long `retry-after`, 5xx and 529, up to CLAUDE_CODE_MAX_RETRIES.
//   - a synthetic `assistant` message (model "<synthetic>") with `error` set,
//     when it gives up. A 403, a 400 and a 429 whose `retry-after` is long are
//     not retried at all: this is the only sign of them.
//   - the `result`, with `is_error` and `api_error_status`.
//
// Neither carries the response headers. A 429 with `retry-after: 60` is
// reported as `api_retry` with `retry_delay_ms: 60000`, but one with
// `retry-after: 120` is not retried and reports no time at all, and the
// `anthropic-ratelimit-*-reset` headers are never passed on. So the time a
// rate limit clears is read by asking the endpoint once more (`probeCredential`):
// a rejected request is not billed, and one that is answered costs a single
// output token.

/**
 * - permanent: the credential itself is refused (revoked, no permission, no
 *   credit). It stays disabled until a person resets it.
 * - rate_limited: usable again at a time the endpoint names.
 * - transient: the endpoint or the network failed; the credential is fine.
 * - other: the request itself, or a limit of ours; another credential would
 *   fail the same way.
 */
export type FailureClass = 'permanent' | 'rate_limited' | 'transient' | 'other'

export type FailureEvidence = {
  /** HTTP status; null for a connection error with no response. */
  status?: number | null | undefined
  category?: SDKAssistantMessageError | undefined
  message?: string | undefined
}

const PERMANENT_CATEGORIES: ReadonlySet<SDKAssistantMessageError> = new Set([
  'authentication_failed',
  'oauth_org_not_allowed',
  'account_on_hold',
  'verification_required',
  'billing_error',
  'cloud_credential_error',
])

/** A 400 that is about the account, not the request: Anthropic's "Your credit balance is too low". */
const BILLING_MESSAGE = /credit balance|billing|payment required|insufficient (?:funds|credit|quota)|quota exceeded/i

export function classifyFailure(evidence: FailureEvidence): FailureClass {
  const { status, category, message = '' } = evidence
  if (category !== undefined && PERMANENT_CATEGORIES.has(category)) return 'permanent'
  if (category === 'rate_limit') return 'rate_limited'
  if (category === 'overloaded' || category === 'server_error') return 'transient'
  if (category === 'invalid_request' || category === 'model_not_found' || category === 'max_output_tokens') {
    return BILLING_MESSAGE.test(message) ? 'permanent' : 'other'
  }
  // `unknown`, or no category: the status decides.
  if (status === 401 || status === 402 || status === 403) return 'permanent'
  if (status === 429) return 'rate_limited'
  if (status === null || status === 408 || status === 409 || (status !== undefined && status >= 500)) {
    return 'transient'
  }
  if (status === 400 && BILLING_MESSAGE.test(message)) return 'permanent'
  return 'other'
}

/**
 * What the user is told about a turn that ended on a failed model request
 * (#1101), from the same evidence `classifyFailure` reads and, for a refusal,
 * what the probe said of the credential (fallback.ts). Claude Code's own
 * "API Error: …" text, when there is one, follows as the detail.
 */
export function describeApiFailure(
  evidence: FailureEvidence,
  judged: { probe?: ProbeVerdict['verdict'] | undefined } = {},
): string {
  const http = typeof evidence.status === 'number' ? `HTTP ${evidence.status}` : 'no response'
  const text = evidence.message?.trim()
  const detail = text ? `: ${text}` : ''
  switch (classifyFailure(evidence)) {
    case 'permanent':
      // A refusal is the key's only once a probe confirms it (fallback.ts).
      if (judged.probe === 'answered') return `the model endpoint refused this request (${http}); the credential itself works${detail}`
      if (judged.probe === 'rate_limited') return `the Claude credential is rate limited (${http}); try again later${detail}`
      if (judged.probe === 'refused') return `the Claude credential was rejected (${http}); check it under Settings → AI${detail}`
      return `the model endpoint refused this request (${http}), and the credential could not be checked; try again, then check it under Settings → AI${detail}`
    case 'rate_limited':
      return `the Claude credential is rate limited (${http}); try again later${detail}`
    case 'transient':
      return `the model endpoint failed (${http}); try again${detail}`
    default:
      return `the model API refused the request (${http})${detail}`
  }
}

/** A rate limit that names no time is tried again after this long. */
export const DEFAULT_COOLDOWN_MS = 60_000
/** No credential waits longer than this without being tried again. */
export const MAX_COOLDOWN_MS = 24 * 60 * 60_000

const ANTHROPIC_BUCKETS = ['requests', 'tokens', 'input-tokens', 'output-tokens'] as const

function parseInstant(value: string | null): number | undefined {
  if (!value) return undefined
  const at = Date.parse(value)
  return Number.isNaN(at) ? undefined : at
}

/**
 * When a rate-limited credential may be used again, from a response's headers
 * (epoch milliseconds), or undefined when they name no time. In order:
 *
 *   1. `retry-after-ms`, then `retry-after` (seconds, or an HTTP date): "the
 *      number of seconds to wait until you can retry the request. Earlier
 *      retries will fail" (https://docs.claude.com/en/api/rate-limits,
 *      "Response headers"). Gateways send it too.
 *   2. the latest `anthropic-ratelimit-{requests,tokens,input-tokens,
 *      output-tokens}-reset` (RFC 3339) of a bucket whose `-remaining` is 0;
 *      failing that, the latest of any bucket (a request too big for what is
 *      left in a bucket that is not empty).
 *   3. `anthropic-ratelimit-unified-reset` (epoch seconds), what a Claude
 *      subscription token is limited by.
 */
export function rateLimitResetFromHeaders(headers: Headers, now: number): number | undefined {
  const ms = Number.parseFloat(headers.get('retry-after-ms') ?? '')
  if (Number.isFinite(ms) && ms > 0) return now + ms
  const retryAfter = headers.get('retry-after')?.trim()
  if (retryAfter) {
    const seconds = Number(retryAfter)
    if (Number.isFinite(seconds) && seconds >= 0) return now + seconds * 1000
    const at = parseInstant(retryAfter)
    if (at !== undefined) return at
  }
  let exhausted: number | undefined
  let any: number | undefined
  for (const bucket of ANTHROPIC_BUCKETS) {
    const at = parseInstant(headers.get(`anthropic-ratelimit-${bucket}-reset`))
    if (at === undefined) continue
    any = Math.max(any ?? at, at)
    if (headers.get(`anthropic-ratelimit-${bucket}-remaining`)?.trim() === '0') exhausted = Math.max(exhausted ?? at, at)
  }
  if (exhausted !== undefined) return exhausted
  if (any !== undefined) return any
  const unified = Number(headers.get('anthropic-ratelimit-unified-reset') ?? '')
  if (Number.isFinite(unified) && unified > 0) return unified * 1000
  return undefined
}

/** `until` (epoch ms), or the default when undefined, held between one second and MAX_COOLDOWN_MS from now. */
export function cooldownUntil(until: number | undefined, now: number): Date {
  const at = until ?? now + DEFAULT_COOLDOWN_MS
  return new Date(Math.min(Math.max(at, now + 1000), now + MAX_COOLDOWN_MS))
}

export type ProbeOptions = {
  /**
   * The model the query used (its init message), for a rate limit, which may
   * be per model. Left out when confirming a refusal: the probe then asks with
   * PROBE_FALLBACK_MODEL, so a refusal scoped to the turn's model (or to a
   * feature it used) does not confirm itself.
   */
  model: string | undefined
  /** The query's signal: an aborted turn stops its probes too. */
  signal?: AbortSignal | undefined
  fetch?: typeof fetch
  resolveHost?: Resolver
  timeoutMs?: number
  now?: () => number
}

/** Used when the query reported no model, and for every refusal check: cheap, and open to every key. */
export const PROBE_FALLBACK_MODEL = 'claude-haiku-4-5'

/**
 * A 403 that is about the key, the account or its billing, not about the
 * request: only such a 403 (or a 401, a 402, a billing 400) confirms a
 * refusal. One that names a model is about the model.
 */
const KEY_REFUSAL = /api[ _-]?key|x-api-key|token|authenticat|organi[sz]ation|account|billing|credit|revoked|disabled|suspended/i

/** Whether a probe's refusal is the key's own (see KEY_REFUSAL). */
export function refusesTheKey(status: number, body: string): boolean {
  if (status === 401 || status === 402) return true
  if (BILLING_MESSAGE.test(body)) return status === 400 || status === 403
  return status === 403 && KEY_REFUSAL.test(body) && !/\bmodel\b/i.test(body)
}
const ANTHROPIC_API = 'https://api.anthropic.com'

/**
 * What the probe found:
 *   - refused: a refusal that is the key's own (`refusesTheKey`);
 *   - answered: 2xx; the credential works now (`until` is a second away);
 *   - rate_limited: 429; `until` is the reset its headers name, else the default;
 *   - unknown: anything else, or no answer; `until` is the default cooldown.
 */
export type ProbeVerdict =
  | { verdict: 'refused'; reason: string }
  | { verdict: 'answered' | 'rate_limited' | 'unknown'; until: Date }

/** A probe that was answered: the limit has cleared (or hit a bucket a one-token request does not). */
export const CLEARED_COOLDOWN_MS = 1000

/**
 * Asks the endpoint once, with a credential a query just found refused or
 * rate limited, what it makes of that credential: a one-token Messages
 * request (fallback.ts uses it to confirm a refusal before disabling, and to
 * read when a rate limit clears). By its status:
 *
 *   - 429: the reset its headers name (`rateLimitResetFromHeaders`), else the default;
 *   - 2xx: answered, so usable again in a second. The `-reset` headers of a
 *     success say when a bucket is full again, not when it can be used;
 *   - 401, 402, or a 400/403 that names the key, account or billing
 *     (`refusesTheKey`): refused outright;
 *   - anything else, or no answer (network, timeout, a gateway host the
 *     egress rules refuse): unknown, with the default cooldown.
 */
export async function probeCredential(credential: Credential, options: ProbeOptions): Promise<ProbeVerdict> {
  const now = options.now ?? Date.now
  // How Claude Code presents an OAuth token to the Messages API is not measured, and
  // in x-api-key Anthropic answers one with a 401 that would read as its own refusal:
  // unknown, so a rate limit or refusal cools it down and never disables it.
  if (credential.kind === 'claude_oauth_token') return { verdict: 'unknown', until: cooldownUntil(undefined, now()) }
  const doFetch = options.fetch ?? fetch
  const base = credential.kind === 'gateway' ? credential.baseUrl : ANTHROPIC_API
  const timeout = AbortSignal.timeout(options.timeoutMs ?? 10_000)
  const signal = options.signal ? AbortSignal.any([timeout, options.signal]) : timeout
  try {
    if (credential.kind === 'gateway') await assertGatewayHostAllowed(base, options.resolveHost ?? systemResolver)
    const res = await doFetch(`${base}/v1/messages`, {
      method: 'POST',
      redirect: 'error',
      signal,
      headers: {
        'content-type': 'application/json',
        'anthropic-version': '2023-06-01',
        ...(credential.kind === 'gateway'
          ? { authorization: `Bearer ${credential.secret}` }
          : { 'x-api-key': credential.secret }),
      },
      body: JSON.stringify({
        model: options.model ?? PROBE_FALLBACK_MODEL,
        max_tokens: 1,
        messages: [{ role: 'user', content: 'ok' }],
      }),
    })
    if (res.status === 401 || res.status === 402 || res.status === 403 || res.status === 400) {
      const body = (await res.text().catch(() => '')).slice(0, 300)
      // Only a refusal that is the key's own (refusesTheKey); any other is about the probe.
      if (refusesTheKey(res.status, body)) {
        return { verdict: 'refused', reason: `the probe was refused (HTTP ${res.status})${body ? `: ${body}` : ''}` }
      }
      return { verdict: 'unknown', until: cooldownUntil(undefined, now()) }
    }
    await res.body?.cancel()
    if (res.ok) return { verdict: 'answered', until: cooldownUntil(now() + CLEARED_COOLDOWN_MS, now()) }
    if (res.status === 429) {
      return { verdict: 'rate_limited', until: cooldownUntil(rateLimitResetFromHeaders(res.headers, now()), now()) }
    }
    return { verdict: 'unknown', until: cooldownUntil(undefined, now()) }
  } catch {
    return { verdict: 'unknown', until: cooldownUntil(undefined, now()) }
  }
}
