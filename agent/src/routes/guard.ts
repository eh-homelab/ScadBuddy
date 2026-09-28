import type { Context } from 'hono'
import { checkOrigin, isSecureTransport, type OriginPolicy, type RequestFacts } from '../http/origins.js'

// Interim gate for OUTWARD-tier writes from Settings (spec §8.1: "outward (send,
// print, delete, settings or credential writes)"). Spec §8.2 wants every
// outward action approved by a human in the ScadBuddy UI; that approval flow is
// #258. Until it lands, credential writes are accepted only when the request
// looks like it came from the UI through the ingress (spec §4.2):
//
//   1. Transport (spec §8.4): a TRUSTED proxy peer (SCADBUDDY_AGENT_TRUSTED_PROXIES)
//      says `X-Forwarded-Proto: https`, or the peer is loopback and no proxy is
//      involved (local development and tests). Forwarded headers from any other
//      peer are ignored (src/http/origins.ts).
//   2. Origin allowlist: `Origin` and the request's own origin (its Host, or a
//      trusted proxy's `X-Forwarded-Host`) must both be the UI's public origin
//      (SCADBUDDY_PUBLIC_URL), or both the same loopback origin from a loopback
//      peer. Comparing Origin with Host alone would not do: under DNS rebinding
//      both carry the attacker's name (src/http/origins.ts explains).
//   3. PUT bodies as `Content-Type: application/json` only. This stops a
//      cross-origin HTML form (which cannot send JSON without a CORS preflight
//      this service never answers); it does nothing against a same-origin or
//      rebound page, which is what 2 is for. (The POST and DELETE routes read
//      no body.)
//
// Browsers send `Origin` on every POST, PUT and DELETE (Fetch standard), so a
// write without one did not come from a page.
//
// The approval decisions (routes/approvals.ts, #258) use the same check: a
// request that passes it is treated as the browser user, the one principal
// that approves outward actions in the UI (spec §8.1, §8.2). The limitation
// below applies to them unchanged.
//
// LIMITATION, stated plainly: this is not an approval and not authentication.
// Anything that can open a TCP connection to port 8081 can set Host and Origin
// to the allowed values; what it cannot do from a non-trusted peer is claim
// HTTPS, so a direct LAN client is refused unless it is on loopback. It stops
// browsers on other origins, rebinding pages, and plain-HTTP clients, not a
// hostile process on the trusted proxy or on the pod's loopback. ScadBuddy's UI
// has no login of its own (spec §8.3, "Stated plainly"); the human approval
// step for outward writes arrives with #258.

export type RemoteAddress = (c: Context) => string | undefined

export function requestFacts(c: Context, remoteAddress: RemoteAddress): RequestFacts {
  return { peer: remoteAddress(c), header: (name) => c.req.header(name) }
}

/** Returns why the request is refused, or undefined when it may proceed. */
export function uiRequestProblem(
  c: Context,
  policy: OriginPolicy,
  remoteAddress: RemoteAddress,
  what = 'credential changes',
): string | undefined {
  const facts = requestFacts(c, remoteAddress)
  if (!isSecureTransport(facts, policy)) {
    return `${what} must come through the HTTPS ingress`
  }
  const verdict = checkOrigin(facts, policy)
  if (!verdict.ok) {
    switch (verdict.reason) {
      case 'no-origin':
        return `${what} must come from the ScadBuddy UI (no Origin header)`
      case 'malformed-origin':
        return `${what} must come from the ScadBuddy UI (malformed Origin header)`
      case 'not-allowed':
        return (
          `${what} must come from the ScadBuddy UI at its public URL ` +
          '(SCADBUDDY_PUBLIC_URL; Origin or Host is not on the allowlist)'
        )
    }
  }
  if (c.req.method === 'PUT') {
    const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
    if (type !== 'application/json') return 'request body must be application/json'
  }
  return undefined
}
