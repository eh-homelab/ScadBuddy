/** How a render in flight is read again: the first wait, the longest, and when to stop. */
export interface PollTiming {
  firstMs: number
  maxMs: number
  limitMs: number
}

/**
 * From 1 s, doubling to 5 s, for at most 10 minutes: a slow render reads at most 12 times a
 * minute, well inside the edge's read limit per client (`storefront/ratelimit.py`), even
 * for a few phones behind one address.
 */
export const POLL: PollTiming = { firstMs: 1_000, maxMs: 5_000, limitMs: 10 * 60_000 }
