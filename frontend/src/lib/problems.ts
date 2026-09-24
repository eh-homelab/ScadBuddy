import type { EligibilityIssue, EligibilityReport, PerPrinterReport, Problem } from '../api/types'

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
 * Bambuddy answers an ineligible pipeline *run* with 409 and a report; the backend passes
 * that body through as the `bambuddy_body` problem extension rather than paraphrasing it.
 * `POST /print/outputs/{id}/eligibility` returns the same report as a 200 instead, which
 * is what the picker reads before anything is printed.
 */
export function eligibilityIssues(problem: Problem): string[] {
  const body = problem.bambuddy_body
  if (typeof body !== 'object' || body === null) return []
  const issues = (body as { issues?: unknown }).issues
  if (!Array.isArray(issues)) return []
  return (issues as EligibilityIssue[]).map(describeIssue)
}

export interface Verdict {
  ok: boolean
  issues: string[]
  printerName?: string
}

/**
 * What to show for one pipeline's report, and whether it blocks.
 *
 * Under `target_kind: "printer_class"` a report's `ok` means *at least one* matching
 * printer passes, and the per-printer reasons live in `printer_reports` while the
 * top-level `issues` carries only class-level problems. Reading `ok` as "every printer is
 * ready" is wrong for that kind — so with a printer chosen this narrows to that printer's
 * reasons, and without one it names whichever printers objected.
 */
export function verdictFor(report: EligibilityReport, printerId?: number | null): Verdict {
  const perPrinter: PerPrinterReport[] = report.printer_reports ?? []
  const classIssues = (report.issues ?? []).map(describeIssue)
  const chosen =
    printerId === null || printerId === undefined
      ? undefined
      : perPrinter.find((entry) => entry.printer_id === printerId)

  if (chosen) {
    return {
      ok: chosen.ok && classIssues.length === 0,
      issues: [...classIssues, ...(chosen.issues ?? []).map(describeIssue)],
      printerName: chosen.printer_name,
    }
  }
  // Every printer that objected, named — listed even when `ok` is true, because under
  // `printer_class` that only means *some* printer passes and the rest are still worth
  // seeing before a fanout run.
  const named = perPrinter
    .filter((entry) => !entry.ok)
    .flatMap((entry) =>
      (entry.issues ?? []).map((issue) => `${entry.printer_name}: ${describeIssue(issue)}`),
    )
  return {
    ok: report.ok,
    issues: [...classIssues, ...named],
    printerName: report.target_printer_name ?? undefined,
  }
}
