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
 *   database is down.
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

/** One status read, classified. Never throws. */
export async function fetchAiAvailability(
  fetchImpl: typeof fetch = (input, init) => fetch(input, init),
): Promise<AiAvailability & { state: AiState }> {
  let response: Response
  try {
    response = await fetchImpl(AI_STATUS_PATH, { headers: { Accept: 'application/json' }, cache: 'no-store' })
  } catch (error) {
    return {
      available: false,
      state: 'unreachable',
      reason: `Could not reach the agent service (${error instanceof Error ? error.message : String(error)}).`,
    }
  }
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
  if (body.state === 'unavailable') return { available: false, state: 'unavailable', reason }
  return { available: false, state: 'not_configured', reason }
}

// One read shared by every caller in the tab (the shell, Settings).
let current: AiAvailability & { state: AiState } = { available: false, state: 'checking' }
let inflight: Promise<void> | null = null
let lastRead = 0
const listeners = new Set<() => void>()

function publish(next: AiAvailability & { state: AiState }) {
  current = next
  for (const listener of listeners) listener()
}

/** Asks the agent again (Settings' "Check again"; also on focus and on a timer while off). */
export function recheckAiAvailability(): Promise<void> {
  inflight ??= fetchAiAvailability()
    .then((next) => {
      lastRead = Date.now()
      publish(next)
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}

/** Tests: forget the shared answer. */
export function resetAiAvailability(next: AiAvailability & { state: AiState } = { available: false, state: 'checking' }) {
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
