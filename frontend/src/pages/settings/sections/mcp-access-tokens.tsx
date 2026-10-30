import { useAiAvailability } from '../../../agent/chat/availability'
import { McpTokensSection } from '../../../components/McpTokensSection'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'mcp-access-tokens'
export const area: AreaId = 'mcp'
export const title = 'Tokens'
export const order = 20
export const hideWhenEmpty = true

/** Mint, list and revoke; shown only where the assistant is available (#251). */
export function Component() {
  const ai = useAiAvailability()
  return (
    <Section id={id} title={title}>
      {ai.available && <McpTokensSection />}
    </Section>
  )
}
