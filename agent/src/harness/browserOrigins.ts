import type { Sql } from 'postgres'
import { normaliseOrigin, OriginConfigError, originPolicy } from '../http/origins.js'

// Where the headless browser (#349, headlessBrowser.ts) may go, and how a
// session remembers the origins a human let it open.
//
// TWO KINDS OF ORIGIN, for a URL a tool takes (`browser_navigate`,
// `browser_tabs` new) and for every request the page makes (the guard,
// headlessBrowser.ts `redirectGuardSource`):
//
//   - ScadBuddy's own, the UI origins: SCADBUDDY_PUBLIC_URL, the backend's
//     stored `public_url` (read each turn) and SCADBUDDY_ALLOWED_ORIGINS
//     (http/origins.ts `originPolicy`, the same list the credential writes and
//     `/mcp` accept). They are opened AS THEY ARE (#983): the model sees them
//     in links, READMEs and what the user pastes, and the pages, cookies and
//     the realtime socket's origin check are the user's. Every request to one
//     carries the agent-actor marker, so the backend's gate (agent_actor.py)
//     refuses outward requests behind the ingress exactly as it did on
//     loopback. When none is configured (a dev run, the tests), the backend's
//     own origin (SCADBUDDY_BACKEND_URL) is the one UI origin. Once one is,
//     the backend's origin is NOT opened, and never approvable: its requests
//     would carry no marker. Matched by exact origin, never by suffix;
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
  /** SCADBUDDY_BACKEND_URL's origin: in `ui` only when listed or nothing else is; never approvable. */
  backend: string
  /** ScadBuddy's own origins, opened as they are; their requests carry the marker. Never empty. */
  ui: readonly string[]
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
  /**
   * The backend's stored `public_url` setting, read when the turn starts so a
   * change applies from the next turn. Ignored unless an http(s) URL.
   */
  livePublicUrl?: string | undefined
  /** SCADBUDDY_ALLOWED_ORIGINS, raw. */
  uiOrigins?: string | undefined
  /** SCADBUDDY_BROWSER_ALLOWED_ORIGINS, raw. */
  browserAllowed?: string | undefined
}): BrowserOrigins {
  const backend = normaliseOrigin(options.backendUrl)
  if (!backend) throw new BrowserOriginsError(`not an http(s) origin: ${options.backendUrl}`)
  const ui = new Set<string>()
  const live = options.livePublicUrl === undefined ? undefined : normaliseOrigin(options.livePublicUrl)
  if (live) ui.add(live)
  try {
    for (const o of originPolicy(options.publicUrl, undefined, options.uiOrigins).publicOrigins) ui.add(o)
  } catch (err) {
    if (err instanceof OriginConfigError) throw new BrowserOriginsError(err.message)
    throw err
  }
  if (ui.size === 0) ui.add(backend)
  const allowed = parseBrowserAllowedOrigins(options.browserAllowed)
  return {
    backend,
    ui: [...ui],
    // The backend and the UI origins are never off-origin.
    allowed: allowed === '*' ? '*' : allowed.filter((o) => o !== backend && !ui.has(o)),
  }
}

/** Whether `origin` may be approved at all. */
export function mayApprove(origins: BrowserOrigins, origin: string): boolean {
  if (origin === origins.backend || origins.ui.includes(origin)) return false
  return origins.allowed === '*' || origins.allowed.includes(origin)
}

/**
 * The paths the ingress routes to the agent itself on ScadBuddy's origin
 * (docs/ai/operating.md §1.1, both `Prefix`). The headless browser never
 * reaches them: a page there could answer the session's own parked approvals
 * (review of #1934). headlessBrowser.ts `redirectGuardSource` holds a copy.
 */
export const AGENT_PATH_PREFIXES = ['/api/v1/ai', '/mcp'] as const

/**
 * Whether a URL's path is, or could be after a proxy normalises it, one of the
 * agent's (AGENT_PATH_PREFIXES). As nginx does before matching a location:
 * percent-escapes decoded (`%2F` too), slashes merged, dot segments resolved;
 * and lower-cased, and backslashes read as slashes, to refuse more rather than
 * less. Anything that does not parse counts as the agent's.
 */
export function isAgentPath(url: string): boolean {
  let path: string
  try {
    path = decodeURIComponent(new URL(url).pathname)
  } catch {
    return true
  }
  const segments: string[] = []
  for (const segment of path.replace(/\\/g, '/').split('/')) {
    if (segment === '..') segments.pop()
    else if (segment !== '' && segment !== '.') segments.push(segment)
  }
  const normal = `/${segments.join('/')}`.toLowerCase()
  return AGENT_PATH_PREFIXES.some((prefix) => normal.startsWith(prefix))
}

export type Navigation =
  /** One of ScadBuddy's own origins: open `url` as it is. */
  | { kind: 'ui'; url: string }
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
    if (origins.ui.includes(origin)) {
      if (!isAgentPath(url.trim())) return { kind: 'ui', url }
      return {
        kind: 'refused',
        reason: `may not open the assistant's own API (${AGENT_PATH_PREFIXES.join(', ')}) on ScadBuddy's origin; ${shown} was not opened.`,
      }
    }
    if (origin === origins.backend) {
      // Its requests would carry the marker only on a UI origin; say where to go instead.
      const u = new URL(url.trim())
      const instead = JSON.stringify(`${origins.ui[0]}${u.pathname}${u.search}${u.hash}`)
      return {
        kind: 'refused',
        reason: `does not open ScadBuddy's backend at ${origin}, only its UI at ${origins.ui.join(', ')}; open ${instead} instead.`,
      }
    }
    if (mayApprove(origins, origin)) return approved.has(origin) ? { kind: 'approved', origin } : { kind: 'ask', origin }
  }
  const ui = origins.ui.join(', ')
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
