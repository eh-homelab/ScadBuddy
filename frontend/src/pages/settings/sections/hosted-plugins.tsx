import { useAiAvailability } from '../../../agent/chat/availability'
import { HostedPluginsPanel } from '../../../components/settings/HostedPlugins'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'hosted-plugins'
export const area: AreaId = 'tools'
export const title = 'Hosted plugins'
export const order = 25
export const hideWhenEmpty = true

/** Claude plugins written and published in ScadBuddy (#6937); offered to the assistant, so shown only where it is available. */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <HostedPluginsPanel />}</Section>
}
