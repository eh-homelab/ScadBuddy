/**
 * msw stand-ins for a remote plugin's OAuth sign-in (#780): the agent's
 * `POST /api/v1/ai/plugins/{name}/oauth/start` and `DELETE …/oauth` (agent/src/routes/plugins.ts).
 * There is no authorization server here: a start answers its link and, as if the person had
 * signed in at once in the window it opens, the plugin reads as signed in from then on, which
 * the Settings card's poll then sees. `failSignIn` makes the next start refuse; `signInRefused`
 * marks a plugin's grant as a revoked refresh does.
 */
import { http, HttpResponse } from 'msw'
import type { SignInStart, SignInStarted } from '../../api/aiPlugins'
import { remotePlugin, setRemotePluginOAuth } from '../aiPlugins'

const base = '/api/v1/ai/plugins'
const ISSUER = 'https://github.com/login/oauth'

let refusal: { status: number; detail: string } | null = null

/** The next start answers this refusal (e.g. 409 "register an OAuth app"). */
export function failSignIn(status: number, detail: string): void {
  refusal = { status, detail }
}

/** The plugin's grant as a revoked refresh leaves it: "Sign in again". */
export function signInRefused(name: string, error = 'its refresh was refused (bad_refresh_token): sign in again'): void {
  const plugin = remotePlugin(name)
  setRemotePluginOAuth(name, {
    status: 'error',
    error,
    issuer: plugin?.oauth?.issuer ?? ISSUER,
    scope: plugin?.oauth?.scope ?? 'repo',
    registration: plugin?.oauth?.registration ?? 'configured',
    refreshes: true,
    expires_at: null,
    connected_at: '2026-11-01T12:00:00.000Z',
  })
}

export function reset(): void {
  refusal = null
}

export const handlers = [
  http.post(`${base}/:name/oauth/start`, async ({ params, request }) => {
    const name = String(params.name)
    const plugin = remotePlugin(name)
    if (!plugin) return HttpResponse.json({ detail: `no plugin named "${name}"` }, { status: 404 })
    if (plugin.auth) {
      return HttpResponse.json(
        { detail: `plugin ${name} has a header credential: remove it (secret: null) before signing in` },
        { status: 409 },
      )
    }
    if (refusal) {
      const { status, detail } = refusal
      refusal = null
      return HttpResponse.json({ detail }, { status })
    }
    const body = (await request.json()) as SignInStart
    const registration = body.client_id ? 'configured' : 'dynamic'
    setRemotePluginOAuth(name, {
      status: 'connected',
      error: null,
      issuer: ISSUER,
      scope: body.scope ?? 'repo read:org',
      registration,
      refreshes: true,
      expires_at: '2026-11-01T20:00:00.000Z',
      connected_at: new Date().toISOString(),
    })
    return HttpResponse.json<SignInStarted>({
      authorize_url: `/api/v1/ai/plugin-oauth/authorize?ticket=mock-${name}`,
      redirect_uri: `${new URL(request.url).origin}/api/v1/ai/plugin-oauth/callback`,
      issuer: ISSUER,
      registration,
      scope: body.scope ?? 'repo read:org',
    })
  }),

  http.delete(`${base}/:name/oauth`, ({ params }) => {
    const name = String(params.name)
    if (!remotePlugin(name)?.oauth) return HttpResponse.json({ detail: `plugin "${name}" is not signed in` }, { status: 404 })
    setRemotePluginOAuth(name, null)
    return new HttpResponse(null, { status: 204 })
  }),
]
