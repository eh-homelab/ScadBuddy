// Which of ScadBuddy's UI origins (browserOrigins.ts `ui`) the headless browser
// reaches without a login. An origin behind an SSO proxy answers every request
// from Chromium, which has no session there, with a redirect to a sign-in page
// on another origin, which the request guard refuses as off-site; measured in
// production: SCADBUDDY_PUBLIC_URL answers 302 to the proxy's sign-in, while an
// internal origin in SCADBUDDY_ALLOWED_ORIGINS answers 200. So each turn asks
// each UI origin's `/healthz` once, following no redirect, and the browser
// prefers an origin that answered 2xx (browserOrigins.ts `browserOrigins`).
//
// Only ever ScadBuddy's configured UI origins are asked (the session manager
// passes browserOrigins.ts `ui`, never a URL from a tool's input), and only a
// bare http(s) origin, at the fixed path, with no redirect followed and a short
// timeout. What an origin answered (hosts, redirect targets, error text) goes to
// the log; the unauthenticated /healthz says only whether any origin is reachable.
import { normaliseOrigin } from '../http/origins.js'

/** One origin's answer: 2xx `ok`; a redirect, 401 or 403 `sign-in`; anything else `unreachable`. */
export type UiReach = { reach: 'ok' } | { reach: 'sign-in' | 'unreachable'; detail: string }

/** The answers for a turn, by normalised origin. An origin left out was not asked. */
export type UiReachMap = Readonly<Record<string, UiReach>>

/** What /healthz shows: whether the last answers include a reachable origin. */
export type UiReachSummary = 'reachable' | 'none reachable' | 'not checked'

export type UiOriginProbeOptions = {
  /** How long an answer is kept. Default 30 s. */
  ttlMs?: number
  /** How long to wait for an answer. Default 2 s, as /healthz waits for the backend. */
  timeoutMs?: number
  now?: () => number
  /** Where a changed answer is reported, with its detail. Default console.warn. */
  log?: (line: string) => void
}

/** Asks UI origins whether they answer without a login, keeping each answer briefly. */
export class UiOriginProbe {
  private readonly ttlMs: number
  private readonly timeoutMs: number
  private readonly now: () => number
  private readonly log: (line: string) => void
  private readonly cache = new Map<string, { at: number; answer: Promise<UiReach> }>()
  private readonly answered = new Map<string, UiReach>()
  /** The latest turn's answers. */
  private latest: UiReachMap | undefined

  constructor(options: UiOriginProbeOptions = {}) {
    this.ttlMs = options.ttlMs ?? 30_000
    this.timeoutMs = options.timeoutMs ?? 2_000
    this.now = options.now ?? Date.now
    this.log = options.log ?? ((line) => console.warn(line))
  }

  /** Each origin's answer, asked at most once per time to live (concurrent turns share it). */
  async probe(origins: readonly string[]): Promise<UiReachMap> {
    const answers = await Promise.all(origins.map(async (origin) => [origin, await this.one(origin)] as const))
    this.latest = Object.fromEntries(answers)
    return this.latest
  }

  /** Whether the origins last asked include a reachable one: all /healthz says. */
  summary(): UiReachSummary {
    if (this.latest === undefined) return 'not checked'
    return Object.values(this.latest).some((a) => a.reach === 'ok') ? 'reachable' : 'none reachable'
  }

  private one(origin: string): Promise<UiReach> {
    const now = this.now()
    const cached = this.cache.get(origin)
    if (cached && now - cached.at <= this.ttlMs) return cached.answer
    const answer = this.ask(origin).then((reach) => {
      const before = this.answered.get(origin)
      if (before?.reach !== reach.reach) {
        this.log(`headless browser: ${origin} answers ${reach.reach}${reach.reach === 'ok' ? '' : ` (${reach.detail})`}`)
      }
      this.answered.set(origin, reach)
      return reach
    })
    this.cache.set(origin, { at: now, answer })
    return answer
  }

  private async ask(origin: string): Promise<UiReach> {
    if (normaliseOrigin(origin) !== origin) return { reach: 'unreachable', detail: 'not an http(s) origin' }
    let response: Response
    try {
      response = await fetch(`${origin}/healthz`, { redirect: 'manual', signal: AbortSignal.timeout(this.timeoutMs) })
    } catch (err) {
      const name = err instanceof Error ? err.name : ''
      if (name === 'TimeoutError' || name === 'AbortError') {
        return { reach: 'unreachable', detail: `no answer within ${this.timeoutMs} ms` }
      }
      const cause = err instanceof Error && err.cause instanceof Error ? err.cause.message : String(err)
      return { reach: 'unreachable', detail: `could not connect: ${cause}` }
    }
    await response.body?.cancel().catch(() => undefined)
    const { status } = response
    if (status >= 200 && status < 300) return { reach: 'ok' }
    if (status >= 300 && status < 400) {
      const to = redirectOrigin(origin, response.headers.get('location'))
      return { reach: 'sign-in', detail: to ? `${status} to ${to}` : `answered ${status}` }
    }
    if (status === 401 || status === 403) return { reach: 'sign-in', detail: `answered ${status}` }
    return { reach: 'unreachable', detail: `answered ${status}` }
  }
}

/** The origin a redirect points at, or undefined; never its path or query. */
function redirectOrigin(base: string, location: string | null): string | undefined {
  if (!location) return undefined
  try {
    const u = new URL(location, base)
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.origin : undefined
  } catch {
    return undefined
  }
}
