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

/**
 * #3856 — how a subject chip reads: the name the server found for it, else its raw
 * reference made legible (a library file, a folder, a project, an output's short id).
 */
export function subjectLabel(subject: { value: string; label?: string | null }): string {
  if (subject.label) return subject.label
  const [prefix, id] = subject.value.split(/:(.*)/s)
  if (id) {
    if (prefix === 'library') return `Library file ${id}`
    if (prefix === 'library-folder') return `Library folder ${id}`
    if (prefix === 'project') return `Project ${id}`
    if (prefix === 'output') return `Output ${id.slice(0, 8)}`
  }
  if (subject.value === 'library-files') return 'Library files'
  return subject.value
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
