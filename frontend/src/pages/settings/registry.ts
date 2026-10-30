import type { ComponentType } from 'react'
import {
  AREAS,
  isAreaId,
  LEGACY_ANCHORS,
  registerSections,
  resolveLocation,
  settingsTarget,
  type AreaId,
  type SettingsLocation,
} from './areas'

// The area list, legacy anchors and location resolving live in ./areas, which imports no
// section, so the agent's entry-chunk code can use them without pulling every section in.
export { AREAS, isAreaId, LEGACY_ANCHORS, resolveLocation, settingsTarget }
export type { AreaId, SettingsLocation }

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

registerSections(SECTION_REGISTRY)
