import type { Context } from 'hono'
import { deleteCookie, getCookie, setCookie } from 'hono/cookie'
import { effectiveRequest, isLoopbackPeer, isSecureTransport, requestOrigin } from '../http/origins.js'
import { OAuthError, randomToken } from '../plugins/oauth.js'
import { COOKIE, FLOW_TTL_MS, OAUTH_PATH } from '../plugins/oauthStore.js'
import { SealError } from '../secrets.js'
import { requestFacts, uiReadProblem } from './guard.js'
import type { RouteModule } from './module.js'

// The browser's half of a plugin's OAuth sign-in (#780; plugins/oauthStore.ts explains
// the flow). Two top-level GETs in the person's browser, on ScadBuddy's own origin (the
// ingress routes /api/v1/ai/* to the agent):
//
//   GET /api/v1/ai/plugin-oauth/authorize?ticket=…   spends the ticket the start route
//       gave, sets the flow's binding cookie, and redirects to the authorization server.
//       A read from the UI (guard.ts `uiReadProblem`): the page opens it with window.open.
//   GET /api/v1/ai/plugin-oauth/callback?code&state&iss   the authorization server's
//       redirect. It comes from another site, so no Origin rule applies: the state (single
//       use, FLOW_TTL_MS) and the cookie the authorize step set in this browser are what
//       admit it. It must still come over the HTTPS ingress, addressed to an allowed origin.
//
// Both answer a small HTML page, never a token, with `Cache-Control: no-store` and
// `Referrer-Policy: no-referrer` (the code and state are in the callback's URL). The
// query string is dropped from traces (telemetry/scrub.ts `url.query`), and nothing here
// logs a URL.

/** Paths the cookie is sent to: these two routes only. */
const COOKIE_PATH = OAUTH_PATH

const PAGE_HEADERS = {
  'cache-control': 'no-store',
  'referrer-policy': 'no-referrer',
  'content-security-policy': "default-src 'none'; frame-ancestors 'none'",
  'x-content-type-options': 'nosniff',
}

function escape(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`)
}

function page(c: Context, status: 200 | 400 | 403 | 409 | 502 | 503, title: string, message: string) {
  const html =
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${escape(title)}</title></head>` +
    `<body><h1>${escape(title)}</h1><p>${escape(message)}</p></body></html>`
  return c.html(html, status, PAGE_HEADERS)
}

/** Why a callback is refused before it is read: not over HTTPS, or not addressed to an allowed origin. */
function callbackProblem(c: Context, deps: Parameters<RouteModule['register']>[1]): string | undefined {
  const facts = requestFacts(c, deps.remoteAddress)
  if (!isSecureTransport(facts, deps.origins)) return 'The sign-in answer must come through the HTTPS ingress.'
  const effective = effectiveRequest(facts, deps.origins)
  const target = effective.scheme ? requestOrigin(effective.scheme, effective.host) : undefined
  const loopback =
    target !== undefined && !effective.viaTrustedProxy && isLoopbackPeer(facts.peer) &&
    ['localhost', '127.0.0.1', '[::1]'].includes(new URL(target).hostname)
  if (target === undefined || (!deps.origins.publicOrigins.has(target) && !loopback)) {
    return 'The sign-in answer must be addressed to ScadBuddy at its public URL.'
  }
  return undefined
}

export const route: RouteModule = {
  register(app, deps) {
    const kek = () => (deps.kek.ok ? deps.kek.kek : undefined)

    app.get(`${OAUTH_PATH}/authorize`, async (c) => {
      const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'a plugin sign-in')
      if (problem) return page(c, 403, 'Sign-in refused', problem)
      const oauth = deps.pluginOAuth
      if (!oauth) return page(c, 503, 'Sign-in unavailable', 'Plugin sign-in needs the AI database.')
      const ticket = c.req.query('ticket')
      if (!ticket) return page(c, 400, 'Sign-in link incomplete', 'This link has no ticket; start the sign-in again from Settings.')
      const binding = randomToken()
      let location: string | undefined
      try {
        location = await oauth.authorize(ticket, binding, kek())
      } catch (err) {
        if (err instanceof SealError) return page(c, 409, 'Sign-in failed', 'The sign-in could not be opened; start it again from Settings.')
        throw err
      }
      if (!location) {
        return page(c, 400, 'Sign-in link expired', 'This sign-in link is unknown, already used or expired; start it again from Settings.')
      }
      setCookie(c, COOKIE, binding, {
        path: COOKIE_PATH,
        httpOnly: true,
        // Lax: sent on the authorization server's top-level redirect back to the callback.
        sameSite: 'Lax',
        secure: effectiveRequest(requestFacts(c, deps.remoteAddress), deps.origins).scheme === 'https',
        maxAge: Math.floor(FLOW_TTL_MS / 1000),
      })
      c.header('cache-control', 'no-store')
      c.header('referrer-policy', 'no-referrer')
      return c.redirect(location, 302)
    })

    app.get(`${OAUTH_PATH}/callback`, async (c) => {
      const problem = callbackProblem(c, deps)
      if (problem) return page(c, 403, 'Sign-in refused', problem)
      const oauth = deps.pluginOAuth
      if (!oauth) return page(c, 503, 'Sign-in unavailable', 'Plugin sign-in needs the AI database.')
      const binding = getCookie(c, COOKIE)
      deleteCookie(c, COOKIE, { path: COOKIE_PATH })
      try {
        const { plugin } = await oauth.complete(
          {
            state: c.req.query('state'),
            code: c.req.query('code'),
            iss: c.req.query('iss'),
            error: c.req.query('error'),
            errorDescription: c.req.query('error_description'),
            binding,
          },
          kek(),
        )
        return page(c, 200, 'Signed in', `Plugin ${plugin} is signed in. You can close this window and go back to ScadBuddy.`)
      } catch (err) {
        if (err instanceof OAuthError) return page(c, err.status, 'Sign-in failed', err.message)
        if (err instanceof SealError) return page(c, 409, 'Sign-in failed', 'The sign-in could not be opened; start it again from Settings.')
        throw err
      }
    })
  },
}
