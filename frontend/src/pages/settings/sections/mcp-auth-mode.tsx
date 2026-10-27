import { useAiAvailability } from '../../../agent/chat/availability'
import { McpAuthSection } from '../../../components/McpAuthSection'
import { mcpAuthChanged } from '../../../lib/mcpAuthChanges'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'mcp-auth-mode'
export const area: AreaId = 'mcp'
export const title = 'Auth mode'
export const order = 10
export const hideWhenEmpty = true

/**
 * The agent service serves these routes, so they show only where the assistant would
 * (#251). A save tells every reader of the mode on the page (#1921).
 */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <McpAuthSection onSaved={() => mcpAuthChanged()} />}</Section>
}
