import type { BackendClient } from '../api/backend.js'
import type { Principal } from '../auth/principal.js'

// Agent authorship of model-history commits (#252: "Each accepted iteration is
// one commit in model history, authored as the agent"). Every backend call a
// tool makes names the principal it runs as, and the session when it runs in
// one; the backend (backend/scadbuddy/core/authorship.py) authors any commit
// that request makes as "ScadBuddy agent", with the two as git trailers, and
// the versions API reports them as `agent` (list_versions).
//
// Not the agent-actor marker (`X-ScadBuddy-Agent-Session`,
// harness/headlessBrowser.ts): that one makes the backend refuse outward routes
// (#349), which is right for the headless browser and wrong for a tool call a
// human already approved.

export const AUTHOR_HEADER = 'X-ScadBuddy-Agent-Author'
export const AUTHOR_SESSION_HEADER = 'X-ScadBuddy-Agent-Author-Session'

/** The backend's bound (core/authorship.py MAX_PRINCIPAL). */
const MAX_PRINCIPAL = 300

/**
 * The backend takes printable ASCII with no spaces; a principal id can hold
 * anything an OIDC issuer or subject does, so the rest is percent-encoded.
 * Cut to the bound a whole character at a time, so the cut never lands inside
 * an escape (review of #741: a `%C3` cut to `%C` would be kept in the history
 * as the principal). `toWellFormed` first, since `encodeURIComponent` throws
 * on a lone surrogate.
 */
export function principalHeader(id: string): string {
  let out = ''
  for (const c of id.toWellFormed()) {
    const encoded = /^[\x21-\x7e]$/u.test(c) ? c : encodeURIComponent(c)
    if (out.length + encoded.length > MAX_PRINCIPAL) break
    out += encoded
  }
  return out || 'unknown'
}

export function authorHeaders(principal: Principal, session?: string): Record<string, string> {
  return {
    [AUTHOR_HEADER]: principalHeader(principal.id),
    ...(session && /^[A-Za-z0-9_-]{1,64}$/.test(session) ? { [AUTHOR_SESSION_HEADER]: session } : {}),
  }
}

type Init = { headers?: Record<string, string | undefined> }
type Method = (url: never, init?: never) => unknown

/**
 * `backend` with `headers` on every call. openapi-fetch takes per-call
 * `headers` (https://openapi-ts.dev/openapi-fetch/api#fetch-options); a
 * header the call sets itself wins.
 */
export function authored(backend: BackendClient, headers: Record<string, string>): BackendClient {
  const wrap =
    <M extends Method>(method: M): M =>
      ((url: never, init?: Init) =>
        (method as unknown as (url: never, init: Init) => unknown)(url, {
          ...init,
          headers: { ...headers, ...init?.headers },
        })) as unknown as M
  return {
    ...backend,
    GET: wrap(backend.GET),
    PUT: wrap(backend.PUT),
    POST: wrap(backend.POST),
    DELETE: wrap(backend.DELETE),
    PATCH: wrap(backend.PATCH),
  }
}
