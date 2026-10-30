import { sectionId } from './sectionNav'

// The settings areas and where a hash or path lands, with no section imported: the
// agent's navigate (entry chunk) needs these, and the registry's eager glob would put
// every Settings section in that chunk (#5016).

export type AreaId =
  | 'general'
  | 'printing'
  | 'library'
  | 'assistant'
  | 'tools'
  | 'mcp'
  | 'audits'
  | 'system'

export const AREAS: readonly { id: AreaId; title: string; servedByAgent: boolean }[] = [
  { id: 'general', title: 'General', servedByAgent: false },
  { id: 'printing', title: 'Printing', servedByAgent: false },
  { id: 'library', title: 'Library', servedByAgent: false },
  { id: 'assistant', title: 'Assistant', servedByAgent: true },
  { id: 'tools', title: 'Tools & plugins', servedByAgent: true },
  { id: 'mcp', title: 'MCP access', servedByAgent: true },
  { id: 'audits', title: 'Audits', servedByAgent: true },
  { id: 'system', title: 'System', servedByAgent: false },
]

export function isAreaId(value: string | undefined): value is AreaId {
  return AREAS.some((area) => area.id === value)
}

/** Where the anchors of the one long page (and the assistant's old sections) now live. */
export const LEGACY_ANCHORS: Readonly<Record<string, { area: AreaId; section: string }>> = {
  assistant: { area: 'assistant', section: 'assistant-credentials' },
  'assistant-browser': { area: 'tools', section: 'builtin' },
  'assistant-tools': { area: 'tools', section: 'builtin' },
  'assistant-model': { area: 'assistant', section: 'model' },
  'assistant-sessions': { area: 'assistant', section: 'default-session-mode' },
  'assistant-mcp': { area: 'mcp', section: 'mcp-auth-mode' },
  'assistant-plugins': { area: 'tools', section: 'packages' },
  plugins: { area: 'tools', section: 'packages' },
  'assistant-audit': { area: 'audits', section: 'audit-log' },
}

export type SettingsLocation = { area: AreaId; hash: string }

export type SectionRef = { id: string; area: AreaId }

let known: readonly SectionRef[] = []

/** The registry tells this module its sections when it loads (the Settings page chunk). */
export function registerSections(sections: readonly SectionRef[]): void {
  known = sections
}

/**
 * Whether the registry has told this module its sections. Until then `known` is empty, so
 * a call without a `sections` argument resolves legacy anchors only (#5021).
 */
export function sectionsLoaded(): boolean {
  return known.length > 0
}

export function settingsTarget(hash: string, sections: readonly SectionRef[] = known): SettingsLocation | null {
  const id = sectionId(hash)
  if (Object.hasOwn(LEGACY_ANCHORS, id)) {
    const { area, section } = LEGACY_ANCHORS[id]!
    return { area, hash: `#${section}` }
  }
  const found = sections.find((section) => section.id === id)
  return found ? { area: found.area, hash: `#${found.id}` } : null
}

export function resolveLocation(
  pathname: string,
  hash: string,
  sections: readonly SectionRef[] = known,
): SettingsLocation {
  const target = settingsTarget(hash, sections)
  if (target) return target
  const segment = pathname.split('/')[2]
  return { area: isAreaId(segment) ? segment : 'general', hash }
}
