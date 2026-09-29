import { ToolError } from './registry.js'

// A bound on the renders an agent starts (#252, "Guardrails": "A render
// timeout and resource limits"). The backend already bounds each render: the
// render timeout, the queue's worker count and the body-size gates in
// backend/scadbuddy/api/limits.py. What it cannot tell apart is a person
// dragging a slider from an agent in a loop, so the agent bounds its own
// callers, per principal (auth/principal.ts), before a render reaches the queue:
//
//   - at most `concurrent` of its renders in flight at once, since render_model
//     waits for each to settle (a second one while the first runs is fine, a
//     fleet of them is a queue nobody else gets into);
//   - at most `perWindow` started in any `windowMs`, so an edit/render loop that
//     never converges stops and says so instead of rendering all night.
//
// A refusal is a ToolError naming the limit and when to try again, which the
// model reads as any other failed call. The counts are in memory: they bound
// a burst, not a total, so there is nothing to keep across a restart and no
// new state in Postgres. The numbers are ScadBuddy's defaults, not settings.

export type RenderLimits = { concurrent: number; perWindow: number; windowMs: number }

export const RENDER_LIMITS: RenderLimits = { concurrent: 2, perWindow: 30, windowMs: 10 * 60_000 }

export class RenderLimiter {
  private readonly inFlight = new Map<string, number>()
  private readonly started = new Map<string, number[]>()

  readonly limits: RenderLimits
  private readonly now: () => number

  constructor(limits: RenderLimits = RENDER_LIMITS, now: () => number = Date.now) {
    this.limits = limits
    this.now = now
  }

  /** Counts a render for `principal`, or throws; call the returned function when it settles. */
  acquire(principal: string): () => void {
    const now = this.now()
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
}

/** The process's limiter, when ToolServices names none (main.ts and tests use this). */
export const DEFAULT_RENDER_LIMITER = new RenderLimiter()
