import type { Diagnostic, Problem, SourceCheck } from '../api/types'

/**
 * One issue of an eligibility report Bambuddy attaches to a refusal, as the backend
 * passes it through in `bambuddy_body`. Not a generated type: it is Bambuddy's shape,
 * not ScadBuddy's, and ScadBuddy's own OpenAPI no longer describes it.
 */
export interface EligibilityIssue {
  kind: string
  slot_index?: number | null
  expected?: string | null
  actual?: string | null
}

/** One line per issue: `filament type mismatch (slot 1): expected PLA, found PETG`. */
export function describeIssue(issue: EligibilityIssue): string {
  const slot =
    issue.slot_index === null || issue.slot_index === undefined
      ? ''
      : ` (slot ${issue.slot_index + 1})`
  const swap =
    issue.expected && issue.actual ? `: expected ${issue.expected}, found ${issue.actual}` : ''
  return `${issue.kind ?? 'blocked'}${slot}${swap}`.replaceAll('_', ' ')
}

/**
 * Bambuddy answers an ineligible send with 409 and a report; the backend passes that
 * body through as the `bambuddy_body` problem extension rather than paraphrasing it.
 */
export function eligibilityIssues(problem: Problem): string[] {
  const body = problem.bambuddy_body
  if (typeof body !== 'object' || body === null) return []
  const issues = (body as { issues?: unknown }).issues
  if (!Array.isArray(issues)) return []
  return (issues as EligibilityIssue[]).map(describeIssue)
}

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
