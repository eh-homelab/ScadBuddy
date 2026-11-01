import { NativeSpools } from '../../../components/settings/NativeSpools'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'native-spools'
export const area: AreaId = 'printing'
export const title = "ScadBuddy's spools"
export const order = 31

/** #4643 — ScadBuddy's own spool inventory, the native spool provider's records. */
export function Component() {
  return (
    <Section
      id={id}
      title={title}
      description="Spools ScadBuddy keeps itself: what is left on each, its RFID tag and its preset. A print sent over the LAN takes what it used off them."
    >
      <NativeSpools />
    </Section>
  )
}
