import { useBambuddyResources } from '../bambuddyResources'
import { Section } from '../controls'
import type { AreaId } from '../registry'
import { RememberedChoicesPanel } from '../RememberedChoicesPanel'

export const id = 'remembered'
export const area: AreaId = 'printing'
export const title = 'Remembered choices'
export const order = 50

/** What the print dialog remembers per model, per printer and per project. */
export function Component() {
  const { targets, projects } = useBambuddyResources()
  return (
    <Section
      id={id}
      title={title}
      description="What the print dialog remembers per model and per printer. Forgetting one leaves the rest; the dialog then opens on its own defaults. A printer with a nozzle rack also lists each hotend: its serial, what it has loaded, what it has printed (which Least used ranks by) and every spool that ran through it."
    >
      <RememberedChoicesPanel targets={targets.data} projects={projects.data} />
    </Section>
  )
}
