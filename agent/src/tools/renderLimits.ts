import { ToolError } from './registry.js'

// A bound on the renders an agent starts (#252, "Guardrails": "A render
// timeout and resource limits"). The backend already bounds each render: the
// render timeout, the queue's worker count and the body-size gates in
// backend/scadbuddy/api/limits.py. What it cannot tell apart is a person
// dragging a slider from an agent in a loop, so the agent bounds its own
// callers, per principal (auth/principal.ts), before a render reaches the queue:
//
//   - at most `concurrent` of its renders in flight at once, counted until the
//     backend job settles (a second one while the first runs is fine, a fleet
//     of them is a queue nobody else gets into). A render handed back still
//     running keeps its slot while render_model polls it in the background, for
//     at most `holdMs` (PR #752 review). Past that the slot is freed whether or
//     not the job has settled: a job that long (a raised SCADBUDDY_RENDER_TIMEOUT
//     times many colours) is bounded only by the backend's shared
//     `render_concurrency`, so this cap is best effort beyond `holdMs`;
//   - at most `perWindow` started in any `windowMs`, so an edit/render loop that
//     never converges stops and says so instead of rendering all night.
//
// A refusal is a ToolError naming the limit and when to try again, which the
// model reads as any other failed call. The counts are in memory: they bound
// a burst, not a total, so there is nothing to keep across a restart and no
// new state in Postgres. A principal with nothing in flight and nothing in the
// window is dropped (anonymous MCP principals are one per session), so the maps
// hold only recent callers. The numbers are ScadBuddy's defaults, not settings.

export type RenderLimits = { concurrent: number; perWindow: number; windowMs: number; holdMs: number }

export const RENDER_LIMITS: RenderLimits = {
  concurrent: 2,
  perWindow: 30,
  windowMs: 10 * 60_000,
  holdMs: 30 * 60_000,
}

export class RenderLimiter {
  private readonly inFlight = new Map<string, number>()
  private readonly started = new Map<string, number[]>()
  private lastSweep = 0

  readonly limits: RenderLimits
  private readonly now: () => number

  constructor(limits: RenderLimits = RENDER_LIMITS, now: () => number = Date.now) {
    this.limits = limits
    this.now = now
  }

  /** Counts a render for `principal`, or throws; call the returned function when it settles. */
  acquire(principal: string): () => void {
    const now = this.now()
    this.sweep(now)
    const recent = (this.started.get(principal) ?? []).filter((t) => t > now - this.limits.windowMs)
    const running = this.inFlight.get(principal) ?? 0
    if (running >= this.limits.concurrent) {
      throw new ToolError(
        `not rendered: ${running} of your renders are still running, the most at once ` +
          `(${this.limits.concurrent}). Wait for one to finish (get_render_job), then try again.`,
      )
    }
    if (recent.length >= this.limits.perWindow) {
      const seconds = Math.ceil((recent[0]! + this.limits.windowMs - now) / 1000)
      this.started.set(principal, recent)
      throw new ToolError(
        `not rendered: ${recent.length} renders in the last ${this.limits.windowMs / 60_000} min is ` +
          `the most allowed. Try again in ${seconds}s, or stop and tell the user what is not converging.`,
      )
    }
    recent.push(now)
    this.started.set(principal, recent)
    this.inFlight.set(principal, running + 1)
    let released = false
    return () => {
      if (released) return
      released = true
      const left = (this.inFlight.get(principal) ?? 1) - 1
      if (left > 0) this.inFlight.set(principal, left)
      else this.inFlight.delete(principal)
    }
  }

  /** The principals the limiter holds counts for. */
  get tracked(): number {
    return new Set([...this.inFlight.keys(), ...this.started.keys()]).size
  }

  /** Drops, at most once a window, every principal with nothing in flight and nothing started in the window. */
  private sweep(now: number): void {
    if (now - this.lastSweep < this.limits.windowMs) return
    this.lastSweep = now
    for (const [principal, times] of this.started) {
      if (!this.inFlight.has(principal) && (times.at(-1) ?? 0) <= now - this.limits.windowMs) {
        this.started.delete(principal)
      }
    }
  }
}

/** The process's limiter, when ToolServices names none (main.ts and tests use this). */
export const DEFAULT_RENDER_LIMITER = new RenderLimiter()
