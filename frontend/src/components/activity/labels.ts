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

/** How much of a project's name a chip shows; a title (backend `TITLE_NAME_CHARS`) as much. */
const SUBJECT_NAME_CHARS = 40

/**
 * #3856 — how a subject chip reads: the name the server found for it, else its raw
 * reference made legible (a library file, a folder, a project by id or by name, an
 * output's short id).
 */
export function subjectLabel(subject: { value: string; label?: string | null }): string {
  if (subject.label) return subject.label
  const [prefix, id] = subject.value.split(/:(.*)/s)
  if (id) {
    // A Bambuddy file is `library:<id>`; an OpenSCAD library's checkout is `library:<name>`.
    // An all-digit library name would read as a file; the two share the prefix with the
    // print records, so a split would rename those too. The server's label, when it has
    // one, says which it is.
    if (prefix === 'library') return /^\d+$/.test(id) ? `Library file ${id}` : `Library ${id}`
    if (prefix === 'library-folder') return `Library folder ${id}`
    if (prefix === 'project') return `Project ${id}`
    // Up to 200 characters, typed by a person: the chip shows the start, its title all.
    if (prefix === 'project-name') {
      return `Project “${id.length > SUBJECT_NAME_CHARS ? `${id.slice(0, SUBJECT_NAME_CHARS)}…` : id}”`
    }
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
