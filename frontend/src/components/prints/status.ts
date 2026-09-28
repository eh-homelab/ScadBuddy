/**
 * Bambuddy's archive statuses as the history names them, plus ScadBuddy's own
 * `deleted_in_bambuddy` for a linked archive Bambuddy no longer has (plan §2.4). Any
 * other status Bambuddy reports is shown as it came.
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
  return STATUS_LABELS[status] ?? status
}
