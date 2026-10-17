import type { ActivityFacets } from '../../api/types'

/** How a principal is shown: the browser is "You", schedules are "System". */
export function principalLabel(principal: string | null, _facets?: ActivityFacets): string {
  if (principal === null) return 'Unknown'
  if (principal === 'browser') return 'You'
  if (principal === 'system') return 'System'
  try {
    return decodeURIComponent(principal)
  } catch {
    return principal
  }
}

/** The `since` choices; the URL keeps the token, the API gets the absolute time. */
export const SINCE_OPTIONS = [
  { value: '24h', label: 'Last 24 hours', hours: 24 },
  { value: '7d', label: 'Last 7 days', hours: 24 * 7 },
  { value: '30d', label: 'Last 30 days', hours: 24 * 30 },
] as const

export function sinceTime(token: string, now = Date.now()): string {
  const option = SINCE_OPTIONS.find((o) => o.value === token)
  return option ? new Date(now - option.hours * 3600_000).toISOString() : ''
}

/**
 * #3747 — how many chips a filter group shows until its "Show more": every kind there
 * is, and every subject seen in the window, buried the list under its own filters.
 */
export const CHIPS_SHOWN = 10
