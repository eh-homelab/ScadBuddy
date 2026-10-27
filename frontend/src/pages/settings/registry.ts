import type { ComponentType } from 'react'
import { sectionId } from './sectionNav'

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

export type SettingsSection = {
  id: string
  area: AreaId
  title: string
  order: number
  Component: ComponentType
  hideWhenEmpty?: boolean
}

export function loadSections(modules: Record<string, unknown>): SettingsSection[] {
  const sections: SettingsSection[] = []
  const seen = new Set<string>()
  for (const [path, module] of Object.entries(modules)) {
    const m = module as Partial<SettingsSection>
    if (
      typeof m.id !== 'string' ||
      typeof m.area !== 'string' ||
      !isAreaId(m.area) ||
      typeof m.title !== 'string' ||
      typeof m.order !== 'number' ||
      typeof m.Component !== 'function'
    ) {
      throw new Error(`settings section ${path} must export id, area, title, order and Component`)
    }
    if (seen.has(m.id)) throw new Error(`duplicate settings section id "${m.id}"`)
    seen.add(m.id)
    sections.push({
      id: m.id,
      area: m.area,
      title: m.title,
      order: m.order,
      Component: m.Component,
      ...(m.hideWhenEmpty === undefined ? {} : { hideWhenEmpty: m.hideWhenEmpty }),
    })
  }
  const rank = (area: AreaId) => AREAS.findIndex((a) => a.id === area)
  return sections.sort((a, b) => rank(a.area) - rank(b.area) || a.order - b.order)
}

export const SECTION_REGISTRY: readonly SettingsSection[] = loadSections(
  import.meta.glob(['./sections/*.tsx', '!./sections/*.test.tsx'], { eager: true }),
)

export function foldIdsOf(sections: readonly SettingsSection[]): string[] {
  return sections.map((section) => section.id)
}

/** Where the anchors of the one long page (and the assistant's old sections) now live. */
export const LEGACY_ANCHORS: Readonly<Record<string, { area: AreaId; section?: string }>> = {
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

export function settingsTarget(
  hash: string,
  sections: readonly SettingsSection[] = SECTION_REGISTRY,
): SettingsLocation | null {
  const id = sectionId(hash)
  if (Object.hasOwn(LEGACY_ANCHORS, id)) {
    const { area, section } = LEGACY_ANCHORS[id]!
    return { area, hash: section ? `#${section}` : '' }
  }
  const found = sections.find((section) => section.id === id)
  return found ? { area: found.area, hash: `#${found.id}` } : null
}

export function resolveLocation(
  pathname: string,
  hash: string,
  sections: readonly SettingsSection[] = SECTION_REGISTRY,
): SettingsLocation {
  const target = settingsTarget(hash, sections)
  if (target) return target
  const segment = pathname.split('/')[2]
  return { area: isAreaId(segment) ? segment : 'general', hash }
}
