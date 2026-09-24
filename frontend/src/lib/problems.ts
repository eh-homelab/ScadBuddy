import type { Problem } from '../api/types'

interface EligibilityIssue {
  kind?: string
  slot_index?: number | null
  expected?: string | null
  actual?: string | null
}

/**
 * Bambuddy answers an ineligible pipeline run with 409 and a report; the backend passes
 * that body through as the `bambuddy_body` problem extension rather than paraphrasing it.
 */
export function eligibilityIssues(problem: Problem): string[] {
  const body = problem.bambuddy_body
  if (typeof body !== 'object' || body === null) return []
  const issues = (body as { issues?: unknown }).issues
  if (!Array.isArray(issues)) return []
  return issues.map((raw) => {
    const issue = raw as EligibilityIssue
    const slot = issue.slot_index === null || issue.slot_index === undefined
      ? ''
      : ` (slot ${issue.slot_index + 1})`
    const swap =
      issue.expected && issue.actual ? `: expected ${issue.expected}, found ${issue.actual}` : ''
    return `${issue.kind ?? 'blocked'}${slot}${swap}`.replaceAll('_', ' ')
  })
}
