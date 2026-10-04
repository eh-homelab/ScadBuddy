import { useEffect, useSyncExternalStore } from 'react'

/**
 * Whether the assistant may render (#256: "hidden when AI is off"), asked of the agent
 * service itself: `GET /api/v1/ai/status` (agent `src/app.ts`, `AiStatusView`). The
 * ingress routes `/api/v1/ai/*` to the agent sidecar on the UI's own origin (AI spec
 * §4.2; `docs/ai/operating.md` §1.1), so the browser asks it directly, with no
 * credential. The agent holds the Claude credential and the browser never does.
 *
 * States:
 * - `configured`: the agent is up and has a Claude credential. The assistant shows.
 * - `not_configured`: the agent answered but is switched off for a setup reason: no
 *   Claude credential yet, no key-encryption key, no database.
 * - `unavailable`: the agent answered but cannot serve, for example because its
 *   database is down, because it was started without its chat socket, or because
 *   this page would be refused by the chat socket's gate (`chat: 'refused'`: opened
 *   by LAN address or over plain HTTP rather than at the public HTTPS URL). The
 *   gate's verdict rides along with any state, `not_configured` too.
 * - `unreachable`: nothing answered as the agent. Either the service is down, or
 *   `/api/v1/ai/*` is not routed to it, in which case the backend's SPA fallback
 *   answers with the app page.
 *
 * The mocked build asks the same route, and msw answers it (`src/mocks/handlers.ts`).
 */
export type AiState = 'checking' | 'configured' | 'not_configured' | 'unavailable' | 'unreachable'

export interface AiAvailability {
  available: boolean
  /** Absent in the test doubles that predate it. */
  state?: AiState
  /** Why it is off, for Settings. */
  reason?: string
  /**
   * The chat socket's gate would refuse this page (opened by LAN address or over
   * plain HTTP). The shell hides a mounted panel on this, and on `not_configured`;
   * `unreachable` and any other `unavailable` may be a passing outage, which the
   * panel's transport rides out by reconnecting.
   */
  chat?: 'refused'
}

export const AI_STATUS_PATH = '/api/v1/ai/status'
/** How often an assistant that is off is asked again (it may have just been set up). */
export const RECHECK_MS = 60_000

/** The agent's answer (agent `AiStatusView`). */
interface StatusBody {
  available: boolean
  state: 'enabled' | 'disabled' | 'unavailable'
  ai: string
  reason?: string
  /** The chat socket's gate would refuse this page's connection. */
  chat?: 'refused'
}

function isStatusBody(value: unknown): value is StatusBody {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.available === 'boolean' &&
    (v.state === 'enabled' || v.state === 'disabled' || v.state === 'unavailable') &&
    typeof v.ai === 'string'
  )
}

const NOT_ROUTED =
  'The agent service did not answer at /api/v1/ai. Check that the ingress routes /api/v1/ai/* ' +
  'and /mcp to the agent sidecar (docs/ai/operating.md §1.1).'

/** How long one status read may take; a proxy that accepts and never answers is unreachable. */
export const STATUS_TIMEOUT_MS = 8_000

/** One status read, classified. Never throws, and settles within `timeoutMs`. */
export async function fetchAiAvailability(
  fetchImpl: typeof fetch = (input, init) => fetch(input, init),
  timeoutMs = STATUS_TIMEOUT_MS,
): Promise<AiAvailability & { state: AiState }> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new DOMException('timed out', 'TimeoutError')), timeoutMs)
  // Also raced, not only aborted: a fetch implementation that ignores the signal
  // must not hold the gate either.
  const deadline = new Promise<never>((_, reject) => {
    controller.signal.addEventListener('abort', () => reject(controller.signal.reason), { once: true })
  })
  try {
    return await Promise.race([read(fetchImpl, controller.signal), deadline])
  } catch (error) {
    const timedOut = controller.signal.aborted
    return {
      available: false,
      state: 'unreachable',
      reason: timedOut
        ? `The agent service did not answer within ${timeoutMs / 1000} s.`
        : `Could not reach the agent service (${error instanceof Error ? error.message : String(error)}).`,
    }
  } finally {
    clearTimeout(timer)
  }
}

async function read(fetchImpl: typeof fetch, signal: AbortSignal): Promise<AiAvailability & { state: AiState }> {
  const response = await fetchImpl(AI_STATUS_PATH, {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    signal,
  })
  if (!response.ok) {
    return {
      available: false,
      state: 'unreachable',
      reason: response.status === 404 ? NOT_ROUTED : `The agent service answered HTTP ${response.status}.`,
    }
  }
  if (!(response.headers.get('content-type') ?? '').includes('application/json')) {
    // The backend's SPA fallback serves index.html for a path it does not know.
    return { available: false, state: 'unreachable', reason: NOT_ROUTED }
  }
  let body: unknown
  try {
    body = await response.json()
  } catch {
    body = undefined
  }
  if (!isStatusBody(body)) return { available: false, state: 'unreachable', reason: NOT_ROUTED }
  if (body.available) return { available: true, state: 'configured' }
  const reason = body.reason ?? body.ai
  // The gate's verdict is about this page, not the agent's health, so it rides along
  // whatever the state (the agent names a setup problem first, but still sends it).
  const chat = body.chat === 'refused' ? { chat: 'refused' as const } : {}
  if (body.state === 'disabled') return { available: false, state: 'not_configured', reason, ...chat }
  // `unavailable`, or `enabled` but not available: refused for this page, or started
  // without its chat socket. Either way the agent answered and cannot serve.
  return { available: false, state: 'unavailable', reason, ...chat }
}

// One read shared by every caller in the tab (the shell, Settings).
let current: AiAvailability & { state: AiState } = { available: false, state: 'checking' }
let inflight: Promise<AiAvailability & { state: AiState }> | null = null
let lastRead = 0
/** Bumped by `resetAiAvailability`, so a read started before it is dropped. */
let generation = 0
const listeners = new Set<() => void>()

function publish(next: AiAvailability & { state: AiState }) {
  current = next
  for (const listener of listeners) listener()
}

/**
 * Asks the agent again (Settings' "Check again"; on focus and on a timer while off;
 * the chat transport after failed handshakes), and resolves with the answer as
 * published: what every `useAiAvailability` sees, or the value in force when the
 * read was dropped by a reset or a forced read.
 */
export function recheckAiAvailability(
  options: { force?: boolean } = {},
): Promise<AiAvailability & { state: AiState }> {
  // `force`: a read that started before a change (a credential save) must not answer for
  // after it, so it is dropped like a reset's and a fresh one starts.
  if (options.force) {
    generation += 1
    inflight = null
  }
  if (inflight) return inflight
  const started = generation
  const read: Promise<AiAvailability & { state: AiState }> = fetchAiAvailability()
    .then((next) => {
      if (started !== generation) return current
      lastRead = Date.now()
      publish(next)
      return next
    })
    .finally(() => {
      if (inflight === read) inflight = null
    })
  inflight = read
  return read
}

/** Tests: forget the shared answer. A read still in flight is ignored when it lands. */
export function resetAiAvailability(next: AiAvailability & { state: AiState } = { available: false, state: 'checking' }) {
  generation += 1
  current = next
  inflight = null
  lastRead = 0
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useAiAvailability(): AiAvailability {
  const value = useSyncExternalStore(subscribe, () => current)
  useEffect(() => {
    if (current.state === 'checking' && !inflight) void recheckAiAvailability()
    const stale = () => !current.available && Date.now() - lastRead >= RECHECK_MS / 2
    const onFocus = () => {
      if (stale()) void recheckAiAvailability()
    }
    const timer = setInterval(() => {
      if (!current.available) void recheckAiAvailability()
    }, RECHECK_MS)
    window.addEventListener('focus', onFocus)
    return () => {
      clearInterval(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [])
  return value
}
