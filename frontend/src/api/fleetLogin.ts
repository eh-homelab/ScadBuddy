/**
 * A person adds a Claude login for the fleet's machines (#7984): core's
 * `POST /api/v1/fleet/logins` (`tools/go/core/internal/secrets/fleet_logins.go`), which Python
 * does not serve, so it is not in `schema.d.ts`. Only a person signed in themselves may call it.
 * The assistant never uses the row; the fleet's machines lease it.
 */
import { request } from './client'

export interface FleetLoginCreate {
  kind: 'claude_login'
  /** The `claudeAiOauth` object of a machine's `~/.claude/.credentials.json`, as JSON text. */
  secret: string
  name?: string
  email?: string
}

/** The stored row, never its secret. */
export interface FleetLoginStored {
  id: string
  kind: string
  scope: string
  name: string | null
  last4: string
  email: string | null
  subscription_type: string | null
}

export function addFleetLogin(body: FleetLoginCreate): Promise<{ credential: FleetLoginStored }> {
  return request('/fleet/logins', { method: 'POST', body: JSON.stringify(body) })
}

/**
 * The `claudeAiOauth` object of what was pasted, as JSON text: the whole `.credentials.json`
 * (`{"claudeAiOauth": {…}}`) or the object itself. Null when it is neither.
 */
export function loginObject(pasted: string): string | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(pasted)
  } catch {
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null
  const inner = (parsed as Record<string, unknown>).claudeAiOauth
  if (inner !== undefined) {
    return typeof inner === 'object' && inner !== null && !Array.isArray(inner) ? JSON.stringify(inner) : null
  }
  return JSON.stringify(parsed)
}
