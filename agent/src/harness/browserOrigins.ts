import type { Sql } from 'postgres'
import { normaliseOrigin, OriginConfigError, originPolicy } from '../http/origins.js'

// Where the headless browser (#349, headlessBrowser.ts) may go, and how a
// session remembers the origins a human let it open.
//
// THREE KINDS OF ORIGIN, for a URL a tool takes (`browser_navigate`,
// `browser_tabs` new) and for every request the page makes (the guard,
// headlessBrowser.ts `redirectGuardSource`):
//
//   - the BACKEND's (SCADBUDDY_BACKEND_URL): it serves the SPA, the one place
//     the browser always may go; only its requests carry the agent-actor marker;
//   - ALIASES of it: the origins the agent already treats as the UI's own,
//     SCADBUDDY_PUBLIC_URL and SCADBUDDY_ALLOWED_ORIGINS (http/origins.ts
//     `originPolicy`, the same list the credential writes and `/mcp` accept).
//     The model sees those in links and READMEs (in production it opened
//     `https://scadbuddy.internal.nullreference.io/m/…` and was refused), so a
//     URL on one is REWRITTEN to the same path, query and fragment on the
//     backend origin rather than refused. The browser never talks to an alias
//     itself: that would go out through the ingress and back, as a stranger;
//   - OFF-ORIGIN: anything else. Refused, as before, unless
//     SCADBUDDY_BROWSER_ALLOWED_ORIGINS (config.ts) lists it or is `*`. An
//     allowed one is an `outward` call the first time in a session: it parks
//     for a human approval (permissions.ts, approvals/service.ts). Once one is
//     approved, the origin is written to `ai_browser_origins` (the migration
//     `*_browser_origins.sql`) and later navigations to it in the same session
//     run without asking again, in this turn and in later ones.
//
// An approved origin is re-checked against the variable on every turn, so
// narrowing the list takes effect even for origins approved before.

/** The browser's origins, normalised (`scheme://host[:port]`, default port dropped). */
export type BrowserOrigins = {
  backend: string
  /** Rewritten to the backend; never the backend itself. */
  aliases: readonly string[]
  /** Off-origin origins that may be approved; `*` for any http(s) origin. Empty: none (the default). */
  allowed: '*' | readonly string[]
}

export class BrowserOriginsError extends Error {
  override name = 'BrowserOriginsError'
}

/**
 * Parses SCADBUDDY_BROWSER_ALLOWED_ORIGINS: unset → none, `*` → any, else a
 * comma-separated list of http(s) origins (a trailing `/` is fine, a path is
 * not). Throws BrowserOriginsError on anything else.
 */
export function parseBrowserAllowedOrigins(raw: string | undefined): '*' | string[] {
  const entries = (raw ?? '').split(',').map((s) => s.trim()).filter(Boolean)
  if (entries.includes('*')) {
    if (entries.length > 1) throw new BrowserOriginsError('SCADBUDDY_BROWSER_ALLOWED_ORIGINS: `*` cannot be combined with origins')
    return '*'
  }
  return entries.map((entry) => {
    const origin = normaliseOrigin(entry)
    let bare = false
    try {
      const url = new URL(entry)
      bare = (url.pathname === '/' || url.pathname === '') && !url.search && !url.hash && !url.username && !url.password
    } catch {
      // not a URL: `origin` is undefined too
    }
    if (!origin || !bare) {
      throw new BrowserOriginsError(`SCADBUDDY_BROWSER_ALLOWED_ORIGINS: "${entry}" is not an http(s) origin`)
    }
    return origin
  })
}

/** The origins for a session's browser, from the service's configuration. */
export function browserOrigins(options: {
  backendUrl: string
  publicUrl?: string | undefined
  /** SCADBUDDY_ALLOWED_ORIGINS, raw. */
  uiOrigins?: string | undefined
  /** SCADBUDDY_BROWSER_ALLOWED_ORIGINS, raw. */
  browserAllowed?: string | undefined
}): BrowserOrigins {
  const backend = normaliseOrigin(options.backendUrl)
  if (!backend) throw new BrowserOriginsError(`not an http(s) origin: ${options.backendUrl}`)
  let ui: ReadonlySet<string>
  try {
    ui = originPolicy(options.publicUrl, undefined, options.uiOrigins).publicOrigins
  } catch (err) {
    if (err instanceof OriginConfigError) throw new BrowserOriginsError(err.message)
    throw err
  }
  const allowed = parseBrowserAllowedOrigins(options.browserAllowed)
  return {
    backend,
    aliases: [...ui].filter((o) => o !== backend),
    // The backend and its aliases are never off-origin.
    allowed: allowed === '*' ? '*' : allowed.filter((o) => o !== backend && !ui.has(o)),
  }
}

/** Whether `origin` may be approved at all. */
export function mayApprove(origins: BrowserOrigins, origin: string): boolean {
  if (origin === origins.backend || origins.aliases.includes(origin)) return false
  return origins.allowed === '*' || origins.allowed.includes(origin)
}

export type Navigation =
  /** On the backend: open `url` (an alias URL already rewritten onto the backend). */
  | { kind: 'backend'; url: string; rewritten: boolean }
  /** Off-origin, and approved in this session. */
  | { kind: 'approved'; origin: string }
  /** Off-origin and allowed, not yet approved in this session: outward. */
  | { kind: 'ask'; origin: string }
  | { kind: 'refused'; reason: string }

/** Where a URL a tool takes would go. */
export function classifyNavigation(url: unknown, origins: BrowserOrigins, approved: ReadonlySet<string>): Navigation {
  const origin = typeof url === 'string' ? normaliseOrigin(url) : undefined
  const shown = typeof url === 'string' ? JSON.stringify(url) : 'that URL'
  if (typeof url === 'string' && origin !== undefined) {
    if (origin === origins.backend) return { kind: 'backend', url, rewritten: false }
    if (origins.aliases.includes(origin)) {
      const u = new URL(url.trim())
      return { kind: 'backend', url: `${origins.backend}${u.pathname}${u.search}${u.hash}`, rewritten: true }
    }
    if (mayApprove(origins, origin)) return approved.has(origin) ? { kind: 'approved', origin } : { kind: 'ask', origin }
  }
  const ui = [origins.backend, ...origins.aliases].join(', ')
  if (origins.allowed !== '*' && origins.allowed.length === 0) {
    return { kind: 'refused', reason: `may only open ScadBuddy's own UI at ${ui}; ${shown} is not on it, so it was not opened.` }
  }
  return {
    kind: 'refused',
    reason:
      `may open ScadBuddy's own UI at ${ui} and, with a human's approval, ` +
      `${origins.allowed === '*' ? 'other http(s) origins' : origins.allowed.join(', ')}; ` +
      `${shown} is none of them, so it was not opened.`,
  }
}

// -- the durable record (ai_browser_origins) ---------------------------------------

/** The origins approved in a session so far, oldest first. */
export async function loadApprovedOrigins(sql: Sql, sessionId: string): Promise<string[]> {
  const rows = await sql<{ origin: string }[]>`
    SELECT origin FROM ai_browser_origins WHERE session_id = ${sessionId} ORDER BY approved_at, origin`
  return rows.map((r) => r.origin)
}

/**
 * Records that a human approved `origin` for this session, naming the
 * approval: one of this session's, decided `approved` and consumed (the
 * navigation ran under it). Throws when there is no such approval, so an origin
 * is never remembered without one. Approving it again is a no-op.
 */
export async function rememberApprovedOrigin(
  sql: Sql,
  sessionId: string,
  origin: string,
  approvalId: string | undefined,
): Promise<void> {
  if (approvalId === undefined) throw new Error(`no approval to remember ${origin} under`)
  const rows = await sql`
    INSERT INTO ai_browser_origins (session_id, origin, approval_id)
    SELECT a.session_id, ${origin}, a.id FROM ai_approvals a
    WHERE a.id = ${approvalId} AND a.session_id = ${sessionId}
      AND a.decision = 'approved' AND a.consumed_at IS NOT NULL
    ON CONFLICT (session_id, origin) DO UPDATE SET session_id = EXCLUDED.session_id
    RETURNING origin`
  if (rows.length !== 1) throw new Error(`approval ${approvalId} is not an approved, used approval in session ${sessionId}`)
}
