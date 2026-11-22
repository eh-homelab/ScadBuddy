import { useAiAvailability } from '../../../agent/chat/availability'
import { PluginFeedSection } from '../../../components/PluginFeedSection'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'mcp-plugin-feed'
export const area: AreaId = 'mcp'
export const title = 'Plugin feed'
export const order = 50
export const hideWhenEmpty = true

/** The marketplace feed Claude Code installs plugins from (#6933); served by the agent, so shown only where it is available. */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <PluginFeedSection />}</Section>
}
