// #4975 phase 8 — the page learning it is signed out (spec 2026-11-01 §5.4, §8.2). With
// SCADBUDDY_AUTH_MODE=enforce a request with no live session is a 401 whose problem type is
// `SIGNED_OUT_PROBLEM` (the backend's `auth/check.py`, the agent's `auth/browserSession.ts`),
// and a socket whose session was revoked closes with `SIGNED_OUT_CLOSE`. Either one notes
// it here; the shell then asks the person to sign in (`components/auth/SignInRequired.tsx`)
// and the sockets wait for the sign-in instead of reconnecting signed out. Other 401s (a
// Bambuddy key refused, say) are not this. Only under `enforce`, as the session read says
// (`lib/signIn.ts` `setEnforced`): in `off` and `observe` the page works signed out, and a
// signed-out answer from a route that needs a person (the person's own sessions) is that
// route's to show.

export const SIGNED_OUT_PROBLEM = 'https://scadbuddy.dev/problems/auth/signed-out'
/** An application close code (RFC 6455 §7.4.2): sign in again, do not reconnect. */
export const SIGNED_OUT_CLOSE = 4401

let signedOut = false
let enforced = false
const listeners = new Set<() => void>()

function emit() {
  for (const listener of [...listeners]) listener()
}

export function isSignedOut(): boolean {
  return signedOut
}

/** What the session read says of SCADBUDDY_AUTH_MODE: whether signed out is refused. */
export function setEnforced(value: boolean): void {
  enforced = value
}

/** The page is signed out; ignored unless signed out is refused (`setEnforced`). */
export function noteSignedOut(): void {
  if (signedOut || !enforced) return
  signedOut = true
  emit()
}

/** Signed in again (the session read says so): whatever waited goes on. */
export function clearSignedOut(): void {
  if (!signedOut) return
  signedOut = false
  emit()
}

export function subscribeSignedOut(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Run `go` once the page is signed in again (at once if it is not signed out). */
export function whenSignedIn(go: () => void): () => void {
  if (!signedOut) {
    go()
    return () => {}
  }
  const listener = () => {
    if (signedOut) return
    listeners.delete(listener)
    go()
  }
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** Whether `response` is the gate's "sign in" answer; reads a clone, so the body stays. */
export async function isSignedOutResponse(response: Response): Promise<boolean> {
  if (response.status !== 401) return false
  try {
    const body = (await response.clone().json()) as { type?: unknown } | null
    return body?.type === SIGNED_OUT_PROBLEM
  } catch {
    return false
  }
}

/** Note `response` if it is the gate's "sign in" answer; the body is left to the caller. */
export function noteIfSignedOut(response: Response): void {
  if (response.status !== 401) return
  void isSignedOutResponse(response).then((signedOut) => {
    if (signedOut) noteSignedOut()
  })
}

/** `fetch` for a caller outside the API client (`api/client.ts` `send` does the same). */
export async function fetchNotingSignedOut(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init)
  noteIfSignedOut(response)
  return response
}

/** For tests. */
export function resetSignedOut(): void {
  signedOut = false
  enforced = false
  listeners.clear()
}
