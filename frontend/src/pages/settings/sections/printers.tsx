import { PrintersPanel } from '../../../components/settings/Printers'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'printers'
export const area: AreaId = 'printing'
export const title = 'Printers'
export const order = 20

/** #3823 — the printer registry. Each change in the panel applies at once, so it is never dirty. */
export function Component() {
  return (
    <Section
      id={id}
      title={title}
      description="One entry per printer, by its serial: the Bambuddy printer it is, and how ScadBuddy reaches it over the LAN. The access code is tested on the printer, stored sealed, and never shown again. Each change applies at once."
    >
      <PrintersPanel />
    </Section>
  )
}
