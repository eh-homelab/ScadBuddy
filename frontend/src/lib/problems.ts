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
 * #6949 K1 (R-K1-23) — every code scadbuddy-core's inventory answers
 * (`tools/go/core/internal/inventory/routes/problems.go` `Codes`; `problems.test.ts` reads
 * that list), each as a sentence for a person. Go's detail is for a log; for the codes in
 * `WITH_DETAIL` it names the field or the reason, so the message adds it.
 */
const INVENTORY_MESSAGES: Record<string, string> = {
  // 404
  kind_not_found: 'That item kind is not there. It may have been merged or deleted.',
  item_not_found: 'That item is not there. It may have been merged or deleted.',
  lot_not_found: 'That lot is not there. It may have been merged or deleted.',
  unit_not_found: 'That unit is not there. It may have been merged or deleted.',
  location_not_found: 'That place is not there. Choose another place.',
  entity_not_found: 'Something this names is not there. It may have been merged or deleted.',
  operation_not_found: 'ScadBuddy has no record of that operation.',
  // 409
  kind_exists: 'An item kind already has that id. Choose another id.',
  kind_in_use: 'The kind has items, or it is built in, so it cannot change like that.',
  kind_archived: 'The item kind is archived. Restore it first.',
  item_exists: 'An item of this kind already has that name. Choose another name.',
  lot_empty: 'That lot has no stock on hand. Take from another lot.',
  unit_installed: 'The unit is fitted in another unit. Take it out first.',
  serial_exists: 'Another unit of this item has that serial.',
  filament_item_refused: 'Filament is kept on the Filament pages, not here.',
  step_not_applicable: 'This item cannot do that: its kind has no such step.',
  step_incomplete: "The item's kind has not set up this step yet. Finish the kind first.",
  handler_not_built: 'ScadBuddy cannot do this step for this kind yet.',
  wrong_counting: 'This item is counted another way: use lots for an item counted in lots, and units for one counted in units.',
  recost_orders_moved: 'Orders kept taking from this lot while it was re-costed. Try again.',
  // 422
  kind_cycle: 'A kind cannot be under itself or under one of its own kinds. Choose another parent.',
  bad_quantity: 'Enter a quantity above 0.',
  bad_property_schema: 'The property keys are not valid.',
  bad_entity_ref: 'Something this names is not written correctly.',
  unknown_entity_kind: 'Something this names is of a kind ScadBuddy does not know.',
  class_open: "The item's kind has not chosen everything it needs yet. Set up the kind first.",
  class_invalid: 'Those kind settings do not go together.',
  bad_limit: 'Ask for 1 to 5000 rows.',
  bad_input: 'Some of the values are not correct. Check them and try again.',
  // 500
  inventory_activity_failed: 'The change stopped before it finished. Check the item before you try again.',
}

/** The codes whose detail names the field or the reason, added to the message. */
const WITH_DETAIL = new Set(['bad_quantity', 'bad_property_schema', 'bad_input', 'class_invalid'])

const name = (ref: unknown) => String(ref).replace(/^item_kind:/, '')
const value = (v: unknown) => (v == null ? 'not set' : String(v))

/** A person's message for an inventory problem, or undefined for a code it does not know. */
export function inventoryProblemMessage(problem: Problem): string | undefined {
  const code = typeof problem.code === 'string' ? problem.code : undefined
  const listed = Array.isArray(problem.problems) ? (problem.problems as Record<string, unknown>[]) : []
  if (code === 'steps_invalid') {
    const lines = listed.map((p) => `${String(p.step)}${p.owner ? ` (in ${name(p.owner)})` : ''}: ${String(p.reason)}.`)
    return ['A step cannot run as set up.', ...lines].join(' ')
  }
  if (code === 'class_changed') {
    // Go sends what changes in `problems: [{owner?, key, from, to}]`; no owner is the kind saved.
    const changes = listed.map((p) => `${p.owner ? `the kind ${name(p.owner)}'s` : "the kind's"} ${String(p.key)} from ${value(p.from)} to ${value(p.to)}`)
    return `This would change ${changes.length ? changes.join(', and ') : "the class of a kind"}, which has items. Its items keep their class.`
  }
  const message = code ? INVENTORY_MESSAGES[code] : undefined
  if (message && code && WITH_DETAIL.has(code) && problem.detail) return `${message} ${problem.detail}`
  return message
}
