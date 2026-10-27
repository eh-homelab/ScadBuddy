import { useBambuddyResources } from '../bambuddyResources'
import { Section } from '../controls'
import { HotendUsagePanel } from '../HotendUsagePanel'
import type { AreaId } from '../registry'

export const id = 'hotend-usage'
export const area: AreaId = 'printing'
export const title = 'Hotend usage'
export const order = 60
export const hideWhenEmpty = true

/** Each rack hotend's use; the panel renders nothing without targets, and the section hides. */
export function Component() {
  const { targets } = useBambuddyResources()
  return (
    <Section id={id} title={title}>
      <HotendUsagePanel targets={targets.data} />
    </Section>
  )
}
