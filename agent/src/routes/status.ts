import type { Hono } from 'hono'
import type { AppDeps } from '../app.js'
import { aiStatus, type AiStatus } from '../aiStatus.js'
import { uiReadProblem } from './guard.js'
import type { RouteModule } from './module.js'

// GET /api/v1/ai/status (#527). Unguarded, like /healthz: it says only what
// /healthz says.

/**
 * GET /api/v1/ai/status: whether the assistant can be offered, for the UI's
 * gate (frontend src/agent/chat/availability.ts). `state` is the AiStatus
 * prefix; `available` also needs the chat socket to exist, and this request
 * to be one the socket's gate would let in; `reason` says why not, in words
 * for Settings. `chat` is that last verdict for the calling request (the
 * transport and origin half of routes/chat.ts's gate, as guard.ts
 * `uiReadProblem` judges a GET), so a page opened by LAN IP or over plain HTTP
 * is told so instead of offering a panel whose socket is refused. It carries
 * nothing /healthz does not, beyond what the caller sent.
 */
export type AiStatusView = {
  available: boolean
  state: 'enabled' | 'disabled' | 'unavailable'
  ai: AiStatus
  reason?: string
  /** Set when this request would be refused by the chat socket's gate. */
  chat?: 'refused'
  /** When no credential is usable now but one is rate limited: the soonest one is usable again (#1093). */
  recovers_at?: string
}

/** The words the UI shows for an AiStatus other than `enabled`. */
export function statusReason(ai: AiStatus, recoversAt?: string): string | undefined {
  if (ai === 'enabled') return undefined
  if (ai === 'disabled (no database)') return 'The agent service has no database (SCADBUDDY_DATABASE_URL is not set).'
  if (ai === 'disabled (no Claude credential)') return 'No Claude credential is configured yet.'
  if (ai === 'unavailable (every Claude credential is rate limited)') {
    return `Every Claude credential is rate limited${recoversAt ? `; the first is usable again at ${recoversAt}` : ''}.`
  }
  if (ai === 'unavailable (no Claude credential is usable now)') {
    return (
      `No Claude credential is usable now: some are rate limited${recoversAt ? ` (the first is usable again at ${recoversAt})` : ''}, ` +
      'and the rest need attention in Settings.'
    )
  }
  if (ai === 'unavailable (every Claude credential is disabled)') {
    return 'Every Claude credential was refused and is disabled; reset one, or save a new secret for it, in Settings.'
  }
  if (ai.startsWith('disabled (no key-encryption key')) {
    return `The agent service has no key-encryption key, so it cannot store a Claude credential (${ai.slice('disabled (no key-encryption key: '.length, -1)}).`
  }
  const inner = ai.replace(/^unavailable \((.*)\)$/, '$1')
  return `The agent service is unavailable: ${inner}.`
}

export function registerStatusRoute(app: Hono, deps: AppDeps): void {
  app.get('/api/v1/ai/status', async (c) => {
    const dbOk = deps.database ? await deps.database.ping() : undefined
    const { ai, recoversAt } = await aiStatus(deps, dbOk)
    const chat = deps.sessions !== undefined && deps.upgradeWebSocket !== undefined
    const state = ai === 'enabled' ? 'enabled' : ai.startsWith('disabled') ? 'disabled' : 'unavailable'
    const refused = uiReadProblem(c, deps.origins, deps.remoteAddress, 'The assistant')
    const reason =
      statusReason(ai, recoversAt) ??
      (chat ? undefined : 'The agent service was started without its chat socket.') ??
      (refused ? `${refused}. Open ScadBuddy at its public HTTPS address to use it.` : undefined)
    const body: AiStatusView = {
      available: ai === 'enabled' && chat && !refused,
      state,
      ai,
      ...(reason ? { reason } : {}),
      ...(refused ? { chat: 'refused' as const } : {}),
      ...(recoversAt ? { recovers_at: recoversAt } : {}),
    }
    c.header('Cache-Control', 'no-store')
    return c.json(body)
  })
}

/** The assistant's availability for the UI's gate (#527). */
export const route: RouteModule = {
  register(app, deps) {
    registerStatusRoute(app, deps)
  },
}
