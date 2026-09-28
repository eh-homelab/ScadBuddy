import type { Diagnostic, Problem, SourceCheck } from '../api/types'

/**
 * A refused save carries the parse check that refused it, as the `diagnostics` and
 * `log_tail` problem extensions — so the editor can mark the same lines the explicit
 * Check button would have, without asking again.
 */
export function refusedCheck(problem: Problem): SourceCheck | undefined {
  const raw = problem.diagnostics
  if (!Array.isArray(raw)) return undefined
  const diagnostics = raw.filter(
    (entry): entry is Diagnostic =>
      typeof entry === 'object' && entry !== null && 'severity' in entry && 'message' in entry,
  )
  const log = problem.log_tail
  return {
    ok: false,
    checked: true,
    timed_out: problem.timed_out === true,
    diagnostics,
    log_tail: Array.isArray(log) ? log.map(String) : [],
  }
}

/**
 * A delete refused because templates of mine are duplicates of this one carries their
 * slugs as the `slugs` problem extension (and the count as `duplicates`).
 */
export function trackingDuplicates(problem: Problem): string[] | undefined {
  const { duplicates, slugs } = problem
  if (typeof duplicates !== 'number' || duplicates < 1) return undefined
  if (!Array.isArray(slugs) || slugs.length === 0) return undefined
  return slugs.map(String)
}
