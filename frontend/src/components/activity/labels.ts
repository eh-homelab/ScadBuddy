import type { ActivityFacets } from '../../api/types'
import { joinList, splitList } from '../../lib/activityQuery'

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

export function chipClass(selected: boolean): string {
  return `rounded-full border px-2.5 py-0.5 text-[12px] transition-colors ${
    selected ? 'border-accent bg-accent/15 text-ink' : 'border-line text-muted hover:text-ink'
  }`
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

/** Adds `value` to, or removes it from, a comma-joined list. */
export function toggleValue(list: string, value: string): string {
  const values = splitList(list)
  return joinList(values.includes(value) ? values.filter((v) => v !== value) : [...values, value])
}
