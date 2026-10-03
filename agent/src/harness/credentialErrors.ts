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
// rate limit clears is read by asking the endpoint once more (`probeRateLimit`):
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
  /** The model the query used (its init message); a rate limit may be per model. */
  model: string | undefined
  fetch?: typeof fetch
  resolveHost?: Resolver
  timeoutMs?: number
  now?: () => number
}

/** Used when the query reported no model. */
export const PROBE_FALLBACK_MODEL = 'claude-haiku-4-5'
const ANTHROPIC_API = 'https://api.anthropic.com'

/**
 * What the probe found: the time the credential is usable again, or that it
 * is refused outright (revoked or out of credit since the 429), which the
 * caller records as permanent.
 */
export type ProbeVerdict = { until: Date } | { refused: string }

/** A probe that was answered: the limit has cleared (or hit a bucket a one-token request does not). */
export const CLEARED_COOLDOWN_MS = 1000

/**
 * Asks the endpoint once, with the credential that was rate limited, when it
 * will take requests again: a one-token Messages request. By its status:
 *
 *   - 429: the reset its headers name (`rateLimitResetFromHeaders`), else the default;
 *   - 2xx: answered, so usable again in a second. The `-reset` headers of a
 *     success say when a bucket is full again, not when it can be used;
 *   - 401, 402, 403: refused outright;
 *   - anything else, or no answer (network, timeout, a gateway host the
 *     egress rules refuse): the default cooldown.
 */
export async function probeRateLimit(credential: Credential, options: ProbeOptions): Promise<ProbeVerdict> {
  const now = options.now ?? Date.now
  const doFetch = options.fetch ?? fetch
  const base = credential.kind === 'gateway' ? credential.baseUrl : ANTHROPIC_API
  const signal = AbortSignal.timeout(options.timeoutMs ?? 10_000)
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
    if (res.status === 401 || res.status === 402 || res.status === 403) {
      const body = (await res.text().catch(() => '')).slice(0, 300)
      return { refused: `the rate-limit probe was refused (HTTP ${res.status})${body ? `: ${body}` : ''}` }
    }
    await res.body?.cancel()
    if (res.ok) return { until: cooldownUntil(now() + CLEARED_COOLDOWN_MS, now()) }
    if (res.status === 429) return { until: cooldownUntil(rateLimitResetFromHeaders(res.headers, now()), now()) }
    return { until: cooldownUntil(undefined, now()) }
  } catch {
    return { until: cooldownUntil(undefined, now()) }
  }
}
