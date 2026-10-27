import { useAiAvailability } from '../../../agent/chat/availability'
import { McpOidcSettings } from '../../../components/McpOidcSettings'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'mcp-oidc'
export const area: AreaId = 'mcp'
export const title = 'OIDC sign-in'
export const order = 30
export const hideWhenEmpty = true

/** Saved on its own: the agent service owns it, not the backend's settings. */
export function Component() {
  const ai = useAiAvailability()
  return (
    <Section id={id} title={title}>
      {ai.available && (
        <div>
          <p className="mb-3 text-sm font-medium">MCP sign-in (OIDC)</p>
          <McpOidcSettings />
        </div>
      )}
    </Section>
  )
}
