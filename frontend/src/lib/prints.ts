/** #311 — how a print's facts read on the page. */

/** The status of a linked archive Bambuddy no longer has (`api/print_history.py`). */
export const DELETED = 'deleted_in_bambuddy'

/** Bambuddy's statuses as words: `completed` → "Completed". */
export function statusLabel(status: string): string {
  if (status === DELETED) return 'Deleted in Bambuddy'
  const words = status.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

export function formatBytes(bytes: number | null | undefined): string | null {
  if (bytes === null || bytes === undefined) return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
