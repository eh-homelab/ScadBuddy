import { createHash } from 'node:crypto'
import type { Context, MiddlewareHandler } from 'hono'
import { getCookie } from 'hono/cookie'
import type { Sql } from 'postgres'
import type { AuthMode } from '../config.js'
import type { BusEvent, EventSource } from '../events/bus.js'
import { BROWSER_USER } from '../routes/approvals.js'
import type { Owner } from '../sessions/protocol.js'

// #4975 phase 5 (spec 2026-11-01 §5.6): the ingress sends `/api/v1/ai/*` to the agent, so
// the agent reads the person's session cookie itself. It looks the cookie's SHA-256 up in
// `auth_sessions_agent`, the backend's view of live `auth_sessions` rows granted to the
// agent's role (principal, name, kind, the expiries; nothing else), caches each answer a few
// seconds, and drops it when the backend publishes `auth.session_revoked`.
//
// The browser user (routes/approvals.ts `BROWSER_USER`, `id: 'browser'`) becomes the
// signed-in person: `kind: 'browser'` stays, `id` is the session's principal. A request
// without a live session is the anonymous browser user, as every request was before sign-in;
// in SCADBUDDY_AUTH_MODE=observe that is logged, and only `enforce` (plan phase 8) refuses it.
// Rows that say `browser` stay shared: every browser-kind principal may act on them (D8,
// sessions/protocol.ts `sameOwner`).

/** The backend's session cookie (`backend/scadbuddy/auth/gate.py` `SESSION_COOKIE`). */
export const SESSION_COOKIE = '__Host-scadbuddy_session'
export const CACHE_MS = 5_000

export type PersonSession = { principal: string; name: string | null; kind: 'login' | 'delegated' | 'break_glass' }

type Cached = { session: PersonSession | undefined; at: number }

export function sessionIdHash(token: string): string {
  return createHash('sha256').update(token, 'utf8').digest('hex')
}

export class BrowserSessions {
  private readonly cache = new Map<string, Cached>()
  private readonly sql: Sql
  private readonly now: () => number

  constructor(sql: Sql, now: () => number = Date.now) {
    this.sql = sql
    this.now = now
  }

  /** The live session `token` names, or undefined (none, expired, revoked or unreadable). */
  async resolve(token: string): Promise<PersonSession | undefined> {
    const id = sessionIdHash(token)
    const cached = this.cache.get(id)
    const now = this.now()
    if (cached && now - cached.at < CACHE_MS) return cached.session
    let session: PersonSession | undefined
    try {
      const rows = await this.sql<PersonSession[]>`
        SELECT principal, name, kind FROM auth_sessions_agent
        WHERE id_hash = ${Buffer.from(id, 'hex')}`
      session = rows[0]
    } catch (error) {
      // The type only: a driver's message can carry the connection's host and user.
      console.error(`could not read a signed-in session (${(error as Error).name})`)
      return undefined
    }
    if (this.cache.size > 1_000) this.cache.clear()
    this.cache.set(id, { session, at: now })
    return session
  }

  /** Forget a revoked session, or (no id) every one of `principal`'s. */
  drop(sessionId: string | undefined, principal: string | undefined): void {
    if (sessionId !== undefined) {
      this.cache.delete(sessionId)
      return
    }
    for (const [id, cached] of this.cache) {
      if (principal === undefined || cached.session?.principal === principal) this.cache.delete(id)
    }
  }

  /** Drops what `auth.session_revoked` names, and everything when events may have been missed. */
  follow(source: EventSource): () => void {
    return source.follow({
      onEvent: (event: BusEvent) => {
        if (event.kind !== 'auth.session_revoked') return
        const principal = typeof event.principal === 'string' ? event.principal : undefined
        this.drop(typeof event.session_id === 'string' ? event.session_id : undefined, principal)
      },
      onResync: () => this.cache.clear(),
    })
  }
}

/** A signed-in person as the owner the routes act for. */
export function personOwner(session: PersonSession): Owner {
  return { kind: 'browser', id: session.principal, label: BROWSER_USER.label }
}

const people = new WeakMap<Request, Owner>()

/** Who the request is from: the signed-in person `personSessions` found, else the browser user. */
export function browserUser(c: Context): Owner {
  return people.get(c.req.raw) ?? BROWSER_USER
}

/** Rate-limited, so a page without a session does not log every request. */
const LOG_INTERVAL_MS = 60_000

/**
 * Resolves the session cookie once per request under `/api/v1/ai/*`, for `browserUser`. A
 * request without a cookie costs no lookup. In `observe` a request without a live session is
 * logged once a minute; nothing is refused in any mode this build accepts.
 */
export function personSessions(
  sessions: BrowserSessions | undefined,
  mode: AuthMode,
  log: (message: string) => void = (message) => console.warn(message),
  now: () => number = Date.now,
): MiddlewareHandler {
  let loggedAt: number | undefined
  return async (c, next) => {
    const token = sessions ? getCookie(c, SESSION_COOKIE) : undefined
    const session = token && sessions ? await sessions.resolve(token) : undefined
    if (session && session.kind !== 'delegated') people.set(c.req.raw, personOwner(session))
    if (!session && mode === 'observe' && (loggedAt === undefined || now() - loggedAt >= LOG_INTERVAL_MS)) {
      loggedAt = now()
      log("enforce would refuse an /api/v1/ai request with no signed-in session; served, as SCADBUDDY_AUTH_MODE=observe")
    }
    await next()
  }
}
