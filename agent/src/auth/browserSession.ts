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
// in SCADBUDDY_AUTH_MODE=observe that is logged, and `enforce` (#4975 phase 8) refuses it with
// the backend's 401 problem (`auth/check.py` `SIGNED_OUT`), the chat and bridge sockets'
// handshakes included. The plugin OAuth callback is the one exception (`UNGATED`): a
// cross-site redirect from an authorization server carries no `SameSite=Strict` cookie, and
// its own state cookie is its credential, as the backend's `/auth/callback` is public.
// Rows that say `browser` stay shared: every browser-kind principal may act on them (D8,
// sessions/protocol.ts `sameOwner`).

/** The backend's session cookie (`backend/scadbuddy/auth/gate.py` `SESSION_COOKIE`). */
export const SESSION_COOKIE = '__Host-scadbuddy_session'
export const CACHE_MS = 5_000

export type PersonSession = { principal: string; name: string | null; kind: 'login' | 'delegated' | 'break_glass' }

type Cached = { session: PersonSession | undefined; at: number }

type Watcher = { id: string; principal: string; revoked: () => void; recheck: () => void }

export function sessionIdHash(token: string): string {
  return createHash('sha256').update(token, 'utf8').digest('hex')
}

export class BrowserSessions {
  private readonly cache = new Map<string, Cached>()
  private readonly watchers = new Set<Watcher>()
  private readonly sql: Sql
  private readonly now: () => number

  constructor(sql: Sql, now: () => number = Date.now) {
    this.sql = sql
    this.now = now
  }

  /** The live session `token` names, or undefined (none, expired, revoked or unreadable). */
  async resolve(token: string): Promise<PersonSession | undefined> {
    try {
      return await this.lookup(sessionIdHash(token))
    } catch {
      return undefined
    }
  }

  /**
   * Whether the session `id` (its hex SHA-256) is still live and still `principal`'s, for a
   * socket opened under it (#5217); undefined when it could not be read.
   */
  async live(id: string, principal: string): Promise<boolean | undefined> {
    try {
      return (await this.lookup(id))?.principal === principal
    } catch {
      return undefined
    }
  }

  /**
   * Tells `watcher` when the session `id` of `principal` is revoked (`revoked`), or when
   * revocations may have been missed (`recheck`). Returns the function that stops it.
   */
  watch(id: string, principal: string, watcher: Pick<Watcher, 'revoked' | 'recheck'>): () => void {
    const entry: Watcher = { id, principal, ...watcher }
    this.watchers.add(entry)
    return () => {
      this.watchers.delete(entry)
    }
  }

  /** The session row `id` names; throws when it cannot be read. */
  private async lookup(id: string): Promise<PersonSession | undefined> {
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
      throw error
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

  /**
   * Drops what `auth.session_revoked` names and tells its watchers; when events may have been
   * missed, drops everything and has every watcher re-check.
   */
  follow(source: EventSource): () => void {
    return source.follow({
      onEvent: (event: BusEvent) => {
        if (event.kind !== 'auth.session_revoked') return
        const principal = typeof event.principal === 'string' ? event.principal : undefined
        const sessionId = typeof event.session_id === 'string' ? event.session_id : undefined
        this.drop(sessionId, principal)
        for (const watcher of [...this.watchers]) {
          if (sessionId !== undefined ? watcher.id === sessionId : watcher.principal === principal) watcher.revoked()
        }
      },
      onResync: () => {
        this.cache.clear()
        for (const watcher of [...this.watchers]) watcher.recheck()
      },
    })
  }
}

/** A signed-in person as the owner the routes act for. */
export function personOwner(session: PersonSession): Owner {
  return { kind: 'browser', id: session.principal, label: BROWSER_USER.label }
}

const people = new WeakMap<Request, Owner>()
const handshakes = new WeakMap<Request, { id: string; principal: string }>()

/** Who the request is from: the signed-in person `personSessions` found, else the browser user. */
export function browserUser(c: Context): Owner {
  return people.get(c.req.raw) ?? BROWSER_USER
}

/**
 * The signed-in session a request acts under (its hex SHA-256 and principal) when
 * `browserUser` is that person: what a socket re-checks while it stays open (#5217).
 */
export function requestSession(c: Context): { id: string; principal: string } | undefined {
  return handshakes.get(c.req.raw)
}

/** Rate-limited, so a page without a session does not log every request. */
const LOG_INTERVAL_MS = 60_000

/** The backend's problem type for a request with no live session (`auth/check.py` `SIGNED_OUT`). */
export const SIGNED_OUT_PROBLEM = 'https://scadbuddy.dev/problems/auth/signed-out'

/** Paths under `/api/v1/ai/*` that `enforce` serves without a session (see the top of this file). */
export const UNGATED: ReadonlySet<string> = new Set(['/api/v1/ai/plugin-oauth/callback'])

/**
 * Resolves the session cookie once per request under `/api/v1/ai/*`, for `browserUser`. A
 * request without a cookie costs no lookup. Without a live session (a delegated one counts),
 * a request is refused with 401 in `enforce` and logged once a minute in `observe`.
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
    if (token && session && session.kind !== 'delegated') {
      people.set(c.req.raw, personOwner(session))
      handshakes.set(c.req.raw, { id: sessionIdHash(token), principal: session.principal })
    }
    const gated = !session && !UNGATED.has(c.req.path)
    if (gated && mode === 'enforce') {
      return c.json(
        {
          type: SIGNED_OUT_PROBLEM,
          title: 'Unauthorized',
          status: 401,
          detail: 'Sign in to ScadBuddy: this request carries no live session.',
        },
        401,
        { 'content-type': 'application/problem+json' },
      )
    }
    if (gated && mode === 'observe' && (loggedAt === undefined || now() - loggedAt >= LOG_INTERVAL_MS)) {
      loggedAt = now()
      log("enforce would refuse an /api/v1/ai request with no signed-in session; served, as SCADBUDDY_AUTH_MODE=observe")
    }
    await next()
  }
}
