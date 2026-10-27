import { useAiAvailability } from '../../../agent/chat/availability'
import { RemotePluginsPanel } from '../../../components/settings/RemotePlugins'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'remote'
export const area: AreaId = 'tools'
export const title = 'Remote MCP plugins'
export const order = 30
export const hideWhenEmpty = true

/** Each action is its own request, applied at once; shown only where the assistant is available. */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <RemotePluginsPanel />}</Section>
}
