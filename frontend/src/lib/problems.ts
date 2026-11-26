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

/**
 * A delete refused because a render of the model is queued or running (#2387), most
 * often the preview its own page started: the `render_in_progress` problem extension.
 */
export function renderInProgress(problem: Problem): boolean {
  return problem.render_in_progress === true
}

/**
 * #6949 K1 (R-K1-23) — the codes scadbuddy-core's inventory answers that a person can act
 * on (`tools/go/core/internal/inventory/routes/problems.go`), each as a sentence. The
 * detail Go writes is for a log, not a person.
 */
const INVENTORY_MESSAGES: Record<string, string> = {
  step_not_applicable: 'This item cannot do that: its kind has no such step.',
  step_incomplete: "The item's kind has not set up this step yet. Finish the kind first.",
  handler_not_built: 'ScadBuddy cannot do this step for this kind yet.',
  wrong_counting: 'This item is counted another way: use lots for an item counted in lots, and units for one counted in units.',
  recost_orders_moved: 'Orders kept taking from this lot while it was re-costed. Try again.',
  kind_in_use: 'The kind has items, or it is built in, so it cannot change like that.',
  class_open: "The item's kind has not chosen everything it needs yet. Set up the kind first.",
  class_invalid: 'Those kind settings do not go together.',
  bad_limit: 'Ask for 1 to 5000 rows.',
  bad_input: 'Some of the values are not correct. Check them and try again.',
}

/** A person's message for an inventory problem, or undefined for a code it does not know. */
export function inventoryProblemMessage(problem: Problem): string | undefined {
  const code = typeof problem.code === 'string' ? problem.code : undefined
  if (code === 'steps_invalid') {
    const steps = Array.isArray(problem.problems) ? (problem.problems as Record<string, unknown>[]) : []
    const lines = steps.map((p) => `${String(p.step)}${p.owner ? ` (from ${String(p.owner)})` : ''}: ${String(p.reason)}`)
    return ['A step cannot run as set up.', ...lines].join(' ')
  }
  if (code === 'class_changed') {
    const { owner, key, from, to } = problem
    return `This changes the kind ${String(owner)}'s ${String(key)} from ${String(from)} to ${String(to)}, which its items do not allow.`
  }
  return code ? INVENTORY_MESSAGES[code] : undefined
}
