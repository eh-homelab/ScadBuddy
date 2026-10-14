import { ToolError } from './registry.js'
import { errorName } from '../telemetry/scrub.js'

// A bound on the renders an agent starts (#252, "Guardrails": "A render
// timeout and resource limits"). The backend already bounds each render: the
// render timeout, the queue's worker count (`render_concurrency`) and the
// body-size gates in backend/scadbuddy/api/limits.py. What it cannot tell apart
// is a person dragging a slider from an agent in a loop, so the agent bounds
// its own callers, per principal (auth/principal.ts), before a render reaches
// the queue:
//
//   - at most `concurrent` of a principal's renders in flight at once (its
//     override in `overrides`, by principal id, else the default), and at most
//     `global` of every principal's together (0: no global bound). A render
//     past either WAITS for a slot, first come first served, so sub-agents
//     rendering a batch queue up instead of retrying in a storm. A wait ends when
//     its caller aborts or after `maxWaitMs` (the tool's own wait, so it ends
//     before the tool's deadline, with nothing submitted), and at most
//     MAX_QUEUED_PER_PRINCIPAL of a principal's renders may wait. A slot is held
//     until the backend job settles; a render handed back still running keeps it
//     while the tool polls in the background, for at most `holdMs` (PR #752
//     review), and past that it is freed whether or not the job settled;
//   - at most `perWindow` started in any `windowMs` (0: no bound), so an
//     edit/render loop that never converges stops and says so. 30 by default; 0 turns it off.
//
// The limits are settings (`ai_settings.render_limits`, routes/renderLimits.ts),
// read through the source at every acquire and release and while anything
// waits, so a change applies without a restart. The counts are in memory: they
// bound a burst, not a total, so there is nothing to keep across a restart. A
// principal with nothing in flight and nothing in the window is dropped
// (anonymous MCP principals are one per session).

export type RenderLimits = {
  /** A principal's renders in flight at once, unless `overrides` names it. At least 1. */
  concurrent: number
  /** Every principal's renders in flight together; 0 is no bound. */
  global: number
  /** Renders a principal may start in any `windowMs`; 0 is no bound. */
  perWindow: number
  windowMs: number
  holdMs: number
  /** `concurrent` for one principal, by its id (`token:<id>`, `oidc:<issuer>#<sub>`, `browser`, `anonymous`). */
  overrides: Readonly<Record<string, number>>
}

export const RENDER_LIMITS: RenderLimits = {
  concurrent: 4,
  global: 0,
  perWindow: 30,
  windowMs: 10 * 60_000,
  holdMs: 30 * 60_000,
  overrides: {},
}

/** The limits now; read at every acquire and release, so a settings change applies at once. */
export type RenderLimitsSource = () => Promise<RenderLimits>

/** The `ai_settings` key (routes/renderLimits.ts). */
export const SETTING_RENDER_LIMITS = 'render_limits'
export const MAX_RENDER_CONCURRENT = 64
export const MAX_RENDER_GLOBAL = 256
export const MAX_RENDER_PER_WINDOW = 10_000
export const MAX_RENDER_OVERRIDES = 200

/** The setting as stored and as the Settings page edits it. */
export type RenderLimitsSetting = {
  per_principal: number
  global: number
  per_window: number
  overrides: { principal: string; concurrent: number }[]
}

export function renderLimitsSetting(limits: RenderLimits): RenderLimitsSetting {
  return {
    per_principal: limits.concurrent,
    global: limits.global,
    per_window: limits.perWindow,
    overrides: Object.entries(limits.overrides).map(([principal, concurrent]) => ({ principal, concurrent })),
  }
}

function count(value: unknown, min: number, max: number, fallback: number): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max ? value : fallback
}

/** The limits a stored setting gives: each field that is missing or out of range keeps its default. */
export function renderLimitsFrom(stored: unknown): RenderLimits {
  if (typeof stored !== 'object' || stored === null) return RENDER_LIMITS
  const s = stored as Partial<Record<keyof RenderLimitsSetting, unknown>>
  const overrides: Record<string, number> = {}
  if (Array.isArray(s.overrides)) {
    for (const entry of s.overrides as unknown[]) {
      if (typeof entry !== 'object' || entry === null) continue
      const { principal, concurrent } = entry as { principal?: unknown; concurrent?: unknown }
      const n = count(concurrent, 1, MAX_RENDER_CONCURRENT, 0)
      if (typeof principal === 'string' && principal !== '' && n > 0) overrides[principal] = n
    }
  }
  return {
    ...RENDER_LIMITS,
    concurrent: count(s.per_principal, 1, MAX_RENDER_CONCURRENT, RENDER_LIMITS.concurrent),
    global: count(s.global, 0, MAX_RENDER_GLOBAL, RENDER_LIMITS.global),
    perWindow: count(s.per_window, 0, MAX_RENDER_PER_WINDOW, RENDER_LIMITS.perWindow),
    overrides,
  }
}

/** The limits as `ai_settings` holds them now, through a settings reader. */
export function settingsRenderLimits(settings: { get<T>(key: string): Promise<T | undefined> }): RenderLimitsSource {
  return async () => renderLimitsFrom(await settings.get<unknown>(SETTING_RENDER_LIMITS))
}

/**
 * The key a principal's renders are counted under. Anonymous MCP principals are
 * one per MCP session (`anonymous:<session id>`), which a caller makes at will, so
 * they share one count: opening another session must not buy another set of slots.
 */
export function renderPrincipal(principal: { id: string; kind: string }): string {
  return principal.kind === 'anonymous' ? 'anonymous' : principal.id
}

/** How often the limits are re-read while a render waits, so a raised one starts it. */
export const RECHECK_MS = 5_000

/**
 * The most of one principal's renders that may wait at once: the queue is in
 * memory, so a caller looping without awaiting must not grow it without end.
 */
export const MAX_QUEUED_PER_PRINCIPAL = 100

type Waiter = { principal: string; grant: (release: () => void) => void; refuse: (err: Error) => void }

export type AcquireOptions = {
  signal?: AbortSignal | undefined
  /** Called once when the render has to wait, with how many are ahead of it. */
  onWait?: (ahead: number) => void
  /** How long it may wait for a slot before it is refused; no bound when unset. */
  maxWaitMs?: number
}

export class RenderLimiter {
  private readonly inFlight = new Map<string, number>()
  private readonly started = new Map<string, number[]>()
  private readonly waiting: Waiter[] = []
  private total = 0
  private lastSweep = Number.NEGATIVE_INFINITY
  private recheck: ReturnType<typeof setInterval> | undefined
  private current: RenderLimits

  private readonly source: RenderLimitsSource
  private readonly now: () => number

  constructor(limits: RenderLimits | RenderLimitsSource = RENDER_LIMITS, now: () => number = () => performance.now()) {
    if (typeof limits === 'function') {
      this.current = RENDER_LIMITS
      this.source = limits
    } else {
      this.current = limits
      this.source = () => Promise.resolve(limits)
    }
    this.now = now
  }

  /** The limits last read. */
  get limits(): RenderLimits {
    return this.current
  }

  /**
   * A slot for one of `principal`'s renders: at once when one is free, else when
   * one frees up. Throws past the window bound (renders waiting count against it),
   * past MAX_QUEUED_PER_PRINCIPAL waiting, or when `signal` aborts.
   * Call the returned function when the render settles.
   */
  async acquire(principal: string, options: AcquireOptions = {}): Promise<() => void> {
    const limits = await this.read()
    const now = this.now()
    this.sweep(now)
    const recent = this.recent(principal, now)
    const own = this.waiting.filter((w) => w.principal === principal).length
    // A render already waiting counts against the window as if it had started:
    // otherwise a burst of calls all pass the check, queue, and each starts later
    // past the bound.
    if (limits.perWindow > 0 && recent.length + own >= limits.perWindow) {
      throw this.windowRefusal(recent, own, limits, now)
    }
    if (own >= MAX_QUEUED_PER_PRINCIPAL) {
      throw new ToolError(
        `not rendered: ${own} of your renders are already waiting for a slot, the most that may queue. ` +
          'Wait for some to finish (get_render_job), then try again.',
      )
    }
    options.signal?.throwIfAborted()
    // First come, first served: nothing overtakes a render of its own principal
    // already waiting, nor, under a global bound, a waiting render that could
    // start now (one held only by its own principal's bound is not ahead).
    // Intended, and not strict FIFO under the global bound (#2847): a waiter held
    // only by its own principal's bound is passed over, and in the gap between a
    // release and `pump()` re-reading the limits, a freed waiter is free but not
    // yet granted, so a newcomer queues behind it rather than taking its slot.
    const ahead =
      own > 0 || (limits.global > 0 && this.waiting.some((w) => this.free(w.principal, limits)))
    if (!ahead && this.free(principal, limits)) return this.start(principal)
    return new Promise<() => void>((resolve, reject) => {
      let timeout: ReturnType<typeof setTimeout> | undefined
      const leave = (err: unknown) => {
        const at = this.waiting.indexOf(waiter)
        if (at >= 0) this.waiting.splice(at, 1)
        settle()
        reject(err)
        // Those behind it may fit now.
        void this.pump()
      }
      const onAbort = () => leave(options.signal?.reason ?? new Error('aborted'))
      const settle = () => {
        options.signal?.removeEventListener('abort', onAbort)
        if (timeout !== undefined) clearTimeout(timeout)
      }
      const waiter: Waiter = {
        principal,
        grant: (release) => {
          settle()
          resolve(release)
        },
        refuse: (err) => {
          settle()
          reject(err)
        },
      }
      if (options.maxWaitMs !== undefined) {
        const waitMs = options.maxWaitMs
        timeout = setTimeout(() => {
          const queued = this.waiting.indexOf(waiter)
          leave(
            new ToolError(
              `not rendered: still waiting for a render slot after ${Math.round(waitMs / 1000)}s ` +
                `(${queued} ahead of it); nothing was submitted. Try again later, or tell the user ` +
                'renders are queued behind others.',
            ),
          )
        }, waitMs)
      }
      options.signal?.addEventListener('abort', onAbort, { once: true })
      this.waiting.push(waiter)
      this.arm()
      options.onWait?.(this.waiting.length - 1)
    })
  }

  /** The principals the limiter holds counts for. */
  get tracked(): number {
    return new Set([...this.inFlight.keys(), ...this.started.keys(), ...this.waiting.map((w) => w.principal)]).size
  }

  /** Renders waiting for a slot. */
  get queued(): number {
    return this.waiting.length
  }

  private async read(): Promise<RenderLimits> {
    try {
      this.current = await this.source()
    } catch (err) {
      // The last limits read stand: a settings read that fails must not stop renders.
      // #1814: only the error's name; its message can quote a connection string or a query.
      console.warn(`render limits: could not read the settings, keeping the last (${errorName(err)})`)
    }
    return this.current
  }

  private windowRefusal(recent: number[], waiting: number, limits: RenderLimits, now: number): ToolError {
    const queued = waiting > 0 ? ` (${waiting} of them still waiting for a slot)` : ''
    // Only renders still waiting fill the window: nothing leaves it until they start.
    const retry =
      recent.length === 0
        ? `Wait for your ${waiting} queued render${waiting === 1 ? '' : 's'} to start (get_render_job), then try again`
        : `Try again in ${Math.max(1, Math.ceil((recent[0]! + limits.windowMs - now) / 1000))}s`
    return new ToolError(
      `not rendered: ${recent.length + waiting} renders in the last ${limits.windowMs / 60_000} min${queued} is ` +
        `the most allowed. ${retry}, or stop and tell the user what is not converging.`,
    )
  }

  private recent(principal: string, now: number): number[] {
    // Only the window's starts are kept, so old ones never pile up (#774).
    const recent = (this.started.get(principal) ?? []).filter((t) => t > now - this.current.windowMs)
    if (recent.length > 0) this.started.set(principal, recent)
    else this.started.delete(principal)
    return recent
  }

  private free(principal: string, limits: RenderLimits): boolean {
    if (limits.global > 0 && this.total >= limits.global) return false
    const own = limits.overrides[principal] ?? limits.concurrent
    return (this.inFlight.get(principal) ?? 0) < Math.max(1, own)
  }

  private start(principal: string): () => void {
    const recent = this.started.get(principal) ?? []
    recent.push(this.now())
    this.started.set(principal, recent)
    this.inFlight.set(principal, (this.inFlight.get(principal) ?? 0) + 1)
    this.total += 1
    let released = false
    return () => {
      if (released) return
      released = true
      const left = (this.inFlight.get(principal) ?? 1) - 1
      if (left > 0) this.inFlight.set(principal, left)
      else this.inFlight.delete(principal)
      this.total -= 1
      void this.pump()
    }
  }

  /** Starts every waiting render that fits, in order, under the limits as they are now. */
  private async pump(): Promise<void> {
    if (this.waiting.length > 0) {
      const limits = await this.read()
      for (let i = 0; i < this.waiting.length; ) {
        if (limits.global > 0 && this.total >= limits.global) break
        const waiter = this.waiting[i]!
        if (this.free(waiter.principal, limits)) {
          this.waiting.splice(i, 1)
          // The window is checked again at the start: it may have been lowered
          // while this render waited.
          const now = this.now()
          const recent = this.recent(waiter.principal, now)
          if (limits.perWindow > 0 && recent.length >= limits.perWindow) {
            waiter.refuse(this.windowRefusal(recent, 0, limits, now))
            continue
          }
          waiter.grant(this.start(waiter.principal))
        } else {
          i += 1
        }
      }
    }
    if (this.waiting.length === 0 && this.recheck !== undefined) {
      clearInterval(this.recheck)
      this.recheck = undefined
    }
  }

  /** While anything waits, the limits are re-read now and then: a raised one starts it. */
  private arm(): void {
    if (this.recheck !== undefined) return
    this.recheck = setInterval(() => void this.pump(), RECHECK_MS)
    this.recheck.unref?.()
  }

  /** Drops, at most once a window, every principal with nothing in flight and nothing started in the window. */
  private sweep(now: number): void {
    if (now - this.lastSweep < this.current.windowMs) return
    this.lastSweep = now
    for (const [principal, times] of this.started) {
      if (!this.inFlight.has(principal) && (times.at(-1) ?? 0) <= now - this.current.windowMs) {
        this.started.delete(principal)
      }
    }
  }
}

/** The process's limiter, when ToolServices names none (tests use this). */
export const DEFAULT_RENDER_LIMITER = new RenderLimiter()
