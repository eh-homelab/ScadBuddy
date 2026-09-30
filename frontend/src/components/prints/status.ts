/**
 * Bambuddy's archive statuses as the history names them, plus ScadBuddy's own
 * `deleted_in_bambuddy` for a linked archive Bambuddy no longer has (plan §2.4). Any
 * other status Bambuddy reports is shown as words: `skipped_objects` → "Skipped objects".
 */
/** The status the prints API gives a linked archive Bambuddy no longer has (#308). */
export const DELETED_STATUS = 'deleted_in_bambuddy'

export const STATUS_LABELS: Record<string, string> = {
  completed: 'Succeeded',
  failed: 'Failed',
  cancelled: 'Cancelled',
  printing: 'Printing',
  [DELETED_STATUS]: 'Deleted in Bambuddy',
}

export function statusLabel(status: string): string {
  const known = STATUS_LABELS[status]
  if (known !== undefined) return known
  const words = status.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}
