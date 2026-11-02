import { HttpResponse, http } from 'msw'
import { resetAuthSession } from '../../lib/signIn'
import type { AuthSessionInfo, AuthSessionView, LoginConfig, LoginConfigUpdate, LoginConfigView } from '../../api/types'

/**
 * #4975 — the sign-in routes. By default the login is off and this browser signed out, so
 * the header shows nothing and every other suite runs as before; a test signs in with
 * `mockSignedIn()` or switches the login on with `mockLoginEnabled()`.
 */

// The issuer and client id are the deployment's (SCADBUDDY_AUTH_OIDC_*), never saved here.
const BLANK: LoginConfig = {
  enabled: false,
  issuer: 'https://idp.example',
  client_id: 'scadbuddy',
  allowed_subjects: [],
  group_claim: null,
  group_value: null,
  rp_logout: false,
}

const ALICE = { principal: 'user:https://idp.example#alice', name: 'Alice', email: 'alice@example.test' }

let config: LoginConfig = { ...BLANK }
let hasSecret = false
let signedIn = false
let sessions: AuthSessionInfo[] = []
let kind: 'login' | 'break_glass' = 'login'

export function mockLoginEnabled(): void {
  config = { ...BLANK, enabled: true, allowed_subjects: ['alice'] }
  hasSecret = true
}

export function mockSignedIn(as: 'login' | 'break_glass' = 'login'): void {
  mockLoginEnabled()
  signedIn = true
  kind = as
  sessions = [
    {
      id: 'a'.repeat(64),
      current: true,
      kind: 'login',
      created_at: '2026-11-01T10:00:00Z',
      last_seen_at: '2026-11-01T12:00:00Z',
      expires_at: '2026-11-15T12:00:00Z',
      client_address: '192.0.2.10',
      user_agent: 'Firefox on Linux',
    },
    {
      id: 'b'.repeat(64),
      current: false,
      kind: 'login',
      created_at: '2026-10-30T10:00:00Z',
      last_seen_at: '2026-10-31T08:00:00Z',
      expires_at: '2026-11-14T08:00:00Z',
      client_address: '192.0.2.11',
      user_agent: 'Safari on iPhone',
    },
  ]
}

export function reset(): void {
  // The page's own copy of the session too, so no test sees the last one's.
  resetAuthSession()
  config = { ...BLANK }
  hasSecret = false
  signedIn = false
  sessions = []
  kind = 'login'
}

function session(): AuthSessionView {
  return {
    signed_in: signedIn,
    user: signedIn ? ALICE : null,
    mode: 'off',
    login_enabled: config.enabled ?? false,
    expires_at: signedIn ? '2026-11-15T12:00:00Z' : null,
    kind: signedIn ? kind : null,
  }
}

function view(): LoginConfigView {
  return {
    config,
    has_client_secret: hasSecret,
    redirect_uri: 'http://localhost/api/v1/auth/callback',
    cannot_enable_reason: null,
  }
}

const signedOut = () =>
  HttpResponse.json(
    { type: 'https://scadbuddy.dev/problems/auth/signed-out', title: 'Unauthorized', status: 401, detail: 'sign in to see your sessions' },
    { status: 401 },
  )

export const handlers = [
  http.get('/api/v1/auth/session', () => HttpResponse.json(session())),
  http.post('/api/v1/auth/logout', () => {
    signedIn = false
    sessions = []
    return HttpResponse.json({ end_session_url: null })
  }),
  http.get('/api/v1/auth/sessions', () => (signedIn ? HttpResponse.json(sessions) : signedOut())),
  http.delete('/api/v1/auth/sessions/:id', ({ params }) => {
    if (!signedIn) return signedOut()
    const gone = sessions.find((s) => s.id === params.id)
    sessions = sessions.filter((s) => s.id !== params.id)
    if (gone?.current) signedIn = false
    return new HttpResponse(null, { status: 204 })
  }),
  http.post('/api/v1/auth/sessions/revoke-all', () => {
    const revoked = sessions.length
    sessions = []
    signedIn = false
    return HttpResponse.json({ revoked })
  }),
  http.get('/api/v1/auth/config', () => HttpResponse.json(view())),
  http.put('/api/v1/auth/config', async ({ request }) => {
    const body = (await request.json()) as LoginConfigUpdate
    if (body.enabled && !(body.allowed_subjects?.length || (body.group_claim && body.group_value))) {
      return HttpResponse.json(
        {
          type: 'https://scadbuddy.dev/problems/auth/incomplete',
          title: 'Unprocessable Content',
          status: 422,
          detail: 'switching sign-in on needs a sign-in rule (subjects, or a group claim and value)',
        },
        { status: 422 },
      )
    }
    const { client_secret: secret, ...rest } = body
    config = { ...BLANK, ...rest }
    if (secret) hasSecret = true
    return HttpResponse.json(view())
  }),
  http.post('/api/v1/auth/config/test', () =>
    HttpResponse.json({ issuer: config.issuer, keys: 2, end_session: true }),
  ),
]
