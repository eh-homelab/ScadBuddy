import type { AppDeps } from './app.js'
import { type OpenerStatus, opensWith, soonestRecovery } from './credentials.js'

/** Upper bound on each step of the status that asks the API (the credential list, its key). */
export const DEFAULT_HEALTH_TIMEOUT_MS = 2000

/**
 * `ai` is `enabled` only when every prerequisite holds; otherwise it names the
 * first one missing, in the order an operator has to fix them. The CI smoke
 * test reads it (.github/workflows/ci.yml asserts the API-unanswered string), and
 * so does the UI's gate, through GET /api/v1/ai/status (`AiStatusView`;
 * frontend src/agent/chat/availability.ts). Keep the strings stable for both.
 */
export type AiStatus =
  | 'enabled'
  | "unavailable (the API does not answer the agent's CRUD areas)"
  | 'unavailable (the API timed out)'
  | 'unavailable (the API did not answer for its key-encryption key)'
  | 'unavailable (the API timed out answering for its key-encryption key)'
  | `disabled (no key-encryption key: ${string})`
  | "unavailable (waiting for the API's input-hash key)"
  | 'disabled (no Claude credential)'
  | 'unavailable (stored credential was sealed with a different key-encryption key)'
  | 'unavailable (stored credential is in an outdated format; save it again)'
  | 'unavailable (every Claude credential is rate limited)'
  | 'unavailable (every Claude credential is disabled)'
  | 'unavailable (no Claude credential is usable now)'

export type CredentialState = 'configured' | 'not configured' | 'unknown'

const TIMED_OUT = Symbol('timed out')

/** `promise`, or TIMED_OUT after `ms`. The promise keeps running; its result is dropped. */
async function within<T>(promise: Promise<T>, ms: number): Promise<T | typeof TIMED_OUT> {
  let timer: NodeJS.Timeout | undefined
  const deadline = new Promise<typeof TIMED_OUT>((resolve) => {
    timer = setTimeout(() => resolve(TIMED_OUT), ms)
  })
  try {
    return await Promise.race([promise, deadline])
  } finally {
    clearTimeout(timer)
  }
}

const UNANSWERED = "unavailable (the API does not answer the agent's CRUD areas)" as const

/**
 * The `ai` and `credential` fields of /healthz, which GET /api/v1/ai/status
 * (routes/status.ts) reports too. In its own module, not app.ts, because a route
 * module must not import a value from app.ts: app.ts loads routes/index.ts, which
 * imports each route module, so that import would be a cycle through a top-level await.
 * The agent's data is the API's (#5299 step 12): `dataOk` is api/dataHealth.ts's `ping`, and
 * `ready` its `ready`, which also waits for the input-hash key the API releases. With the areas
 * answering but no key, the routes and /mcp answer 503, and `ai` says so once the API holds its
 * KEK (with none, the key never comes and `ai` names the missing KEK instead).
 */
export async function aiStatus(
  deps: Pick<AppDeps, 'credentials' | 'healthTimeoutMs'>,
  dataOk: boolean,
  ready = true,
): Promise<{ ai: AiStatus; credential: CredentialState; recoversAt?: string }> {
  if (!dataOk) return { ai: UNANSWERED, credential: 'unknown' }
  // Each step is bounded: a liveness probe must answer well inside its own timeout.
  const timeoutMs = deps.healthTimeoutMs ?? DEFAULT_HEALTH_TIMEOUT_MS
  let list
  try {
    list = await within(deps.credentials.list(), timeoutMs)
  } catch {
    return { ai: UNANSWERED, credential: 'unknown' }
  }
  if (list === TIMED_OUT) return { ai: 'unavailable (the API timed out)', credential: 'unknown' }
  const credential = list.length ? 'configured' : 'not configured'
  // The key that opens them is the API's (#5299 D5).
  let opener: OpenerStatus | typeof TIMED_OUT
  try {
    opener = await within(deps.credentials.opener(), timeoutMs)
  } catch {
    return { ai: 'unavailable (the API did not answer for its key-encryption key)', credential }
  }
  if (opener === TIMED_OUT) return { ai: 'unavailable (the API timed out answering for its key-encryption key)', credential }
  const kek: OpenerStatus = opener
  if (!kek.ok) return { ai: `disabled (no key-encryption key: ${kek.reason})`, credential }
  if (!ready) return { ai: "unavailable (waiting for the API's input-hash key)", credential }
  const kekId = kek.kek.id
  if (!list.length) return { ai: 'disabled (no Claude credential)', credential }
  const opening = list.filter((c) => opensWith(c, kek))
  if (!opening.length) {
    return list.some((c) => c.kekId !== kekId)
      ? { ai: 'unavailable (stored credential was sealed with a different key-encryption key)', credential }
      : { ai: 'unavailable (stored credential is in an outdated format; save it again)', credential }
  }
  // #1093: usable now, or when the first one will be again. "Rate limited"
  // only when that is all of them; a mix (some disabled, or some the key
  // cannot open) is "not usable now", with the time one recovers if any will.
  if (opening.some((c) => c.status === 'active')) return { ai: 'enabled', credential }
  const soonest = soonestRecovery(list, kek)
  // "Disabled" only when a reset would fix every one: none sealed under another
  // key or in the old format, which a reset does not open.
  if (!soonest) {
    return list.every((c) => c.status === 'disabled' && opensWith(c, kek))
      ? { ai: 'unavailable (every Claude credential is disabled)', credential }
      : { ai: 'unavailable (no Claude credential is usable now)', credential }
  }
  const recoversAt = soonest.toISOString()
  return list.every((c) => c.status === 'cooling_down' && opensWith(c, kek))
    ? { ai: 'unavailable (every Claude credential is rate limited)', credential, recoversAt }
    : { ai: 'unavailable (no Claude credential is usable now)', credential, recoversAt }
}
