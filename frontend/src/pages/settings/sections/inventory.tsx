import { Section } from '../controls'
import type { AreaId } from '../registry'
import { SpoolInventoryPanel } from '../SpoolInventoryPanel'

export const id = 'inventory'
export const area: AreaId = 'printing'
export const title = 'Spool inventory'
export const order = 30

/** Bambuddy's spools, read only. */
export function Component() {
  return (
    <Section
      id={id}
      title={title}
      description="Bambuddy's spools, read only: what is left on each, what it cost and where it is loaded. Spools are added, weighed and edited in Bambuddy."
    >
      <SpoolInventoryPanel />
    </Section>
  )
}
