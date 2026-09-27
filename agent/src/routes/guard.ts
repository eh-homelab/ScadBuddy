import type { Context } from 'hono'

// Interim gate for OUTWARD-tier writes from Settings (spec §8.1: "outward (send,
// print, delete, settings or credential writes)"). Spec §8.2 wants every
// outward action approved by a human in the ScadBuddy UI; that approval flow is
// #258. Until it lands, credential writes are accepted only when the request
// looks like it came from the UI through the ingress (spec §4.2):
//
//   1. Transport (spec §8.4): `X-Forwarded-Proto: https` set by the ingress
//      that terminates TLS, or a loopback peer (local development and tests).
//   2. Same origin: an `Origin` header whose host equals the request's host
//      (`X-Forwarded-Host` when the ingress sets it). Browsers always send
//      Origin on a cross-origin request and on same-origin POST/PUT/DELETE, so
//      a page on another origin cannot drive these routes from a user's
//      browser (CSRF), and a request without Origin is not from a browser.
//   3. PUT bodies as `Content-Type: application/json` only, which a
//      cross-origin page cannot send without a CORS preflight this service
//      never answers. (The POST and DELETE routes read no body.)
//
// LIMITATION, stated plainly: this is not an approval and not authentication.
// Headers are set by the client, so anything that can reach the agent's port
// directly (another pod, a LAN client when the port is exposed) can forge all
// three. It stops browsers on other origins, not a hostile client with network
// access. ScadBuddy's UI has no login of its own (spec §8.3, "Stated plainly");
// the human approval step for outward writes arrives with #258.

export type RemoteAddress = (c: Context) => string | undefined

const LOOPBACK = /^(127\.\d+\.\d+\.\d+|::1|::ffff:127\.\d+\.\d+\.\d+)$/

export function isLoopback(address: string | undefined): boolean {
  return address !== undefined && LOOPBACK.test(address)
}

/** Returns why the request is refused, or undefined when it may proceed. */
export function uiRequestProblem(c: Context, remoteAddress: RemoteAddress): string | undefined {
  const proto = c.req.header('x-forwarded-proto')?.split(',')[0]?.trim().toLowerCase()
  if (proto !== 'https' && !(proto === undefined && isLoopback(remoteAddress(c)))) {
    return 'credential changes must come through the HTTPS ingress'
  }
  const origin = c.req.header('origin')
  if (!origin) return 'credential changes must come from the ScadBuddy UI (no Origin header)'
  let originHost: string
  try {
    originHost = new URL(origin).host
  } catch {
    return 'credential changes must come from the ScadBuddy UI (malformed Origin header)'
  }
  const host = c.req.header('x-forwarded-host')?.split(',')[0]?.trim() ?? c.req.header('host')
  if (!host || originHost.toLowerCase() !== host.toLowerCase()) {
    return 'credential changes must come from the ScadBuddy UI (cross-origin request)'
  }
  if (c.req.method === 'PUT') {
    const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
    if (type !== 'application/json') return 'request body must be application/json'
  }
  return undefined
}
