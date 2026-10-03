import type { AppDeps } from './app.js'
import { opensWith, soonestRecovery } from './credentials.js'

/** Upper bound on each database step of the status (migrations, credential read). */
export const DEFAULT_HEALTH_TIMEOUT_MS = 2000

/**
 * `ai` is `enabled` only when every prerequisite holds; otherwise it names the
 * first one missing, in the order an operator has to fix them. The CI smoke
 * test reads it (.github/workflows/ci.yml asserts the no-database string), and
 * so does the UI's gate, through GET /api/v1/ai/status (`AiStatusView`;
 * frontend src/agent/chat/availability.ts). Keep the strings stable for both.
 */
export type AiStatus =
  | 'enabled'
  | 'disabled (no database)'
  | 'unavailable (database unreachable)'
  | 'unavailable (database migrations failed)'
  | 'unavailable (database timed out)'
  | `disabled (no key-encryption key: ${string})`
  | 'disabled (no Claude credential)'
  | 'unavailable (stored credential was sealed with a different key-encryption key)'
  | 'unavailable (stored credential is in an outdated format; save it again)'
  | 'unavailable (every Claude credential is rate limited)'
  | 'unavailable (every Claude credential is disabled)'

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

/**
 * The `ai` and `credential` fields of /healthz, which GET /api/v1/ai/status
 * (routes/status.ts) reports too. In its own module, not app.ts, because a route
 * module must not import a value from app.ts: app.ts loads routes/index.ts, which
 * imports each route module, so that import would be a cycle through a top-level await.
 */
export async function aiStatus(
  deps: Pick<AppDeps, 'database' | 'credentials' | 'kek' | 'healthTimeoutMs'>,
  dbOk: boolean | undefined,
): Promise<{ ai: AiStatus; credential: CredentialState; recoversAt?: string }> {
  if (dbOk === undefined || !deps.database || !deps.credentials) {
    return { ai: 'disabled (no database)', credential: 'unknown' }
  }
  if (!dbOk) return { ai: 'unavailable (database unreachable)', credential: 'unknown' }
  // Both steps are bounded: ready() may be waiting on the migration advisory
  // lock (bounded itself by lock_timeout, db/migrations.ts), and a liveness
  // probe must answer well inside its own timeout regardless.
  const timeoutMs = deps.healthTimeoutMs ?? DEFAULT_HEALTH_TIMEOUT_MS
  const ready = await within(deps.database.ready(), timeoutMs)
  if (ready === TIMED_OUT) return { ai: 'unavailable (database timed out)', credential: 'unknown' }
  if (!ready) return { ai: 'unavailable (database migrations failed)', credential: 'unknown' }
  let list
  try {
    list = await within(deps.credentials.list(), timeoutMs)
  } catch {
    return { ai: 'unavailable (database unreachable)', credential: 'unknown' }
  }
  if (list === TIMED_OUT) return { ai: 'unavailable (database timed out)', credential: 'unknown' }
  const credential = list.length ? 'configured' : 'not configured'
  if (!deps.kek.ok) return { ai: `disabled (no key-encryption key: ${deps.kek.reason})`, credential }
  const kekId = deps.kek.kek.id
  if (!list.length) return { ai: 'disabled (no Claude credential)', credential }
  const opening = list.filter((c) => opensWith(c, deps.kek))
  if (!opening.length) {
    return list.some((c) => c.kekId !== kekId)
      ? { ai: 'unavailable (stored credential was sealed with a different key-encryption key)', credential }
      : { ai: 'unavailable (stored credential is in an outdated format; save it again)', credential }
  }
  // #1093: usable now, or when the first one will be again.
  if (opening.some((c) => c.status === 'active')) return { ai: 'enabled', credential }
  const soonest = soonestRecovery(list, deps.kek)
  return soonest
    ? { ai: 'unavailable (every Claude credential is rate limited)', credential, recoversAt: soonest.toISOString() }
    : { ai: 'unavailable (every Claude credential is disabled)', credential }
}
