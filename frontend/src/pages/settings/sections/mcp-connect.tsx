import { useAiAvailability } from '../../../agent/chat/availability'
import { api } from '../../../api/client'
import { McpConnectSnippet } from '../../../components/McpConnectSnippet'
import { useMcpAuthVersion } from '../../../lib/mcpAuthChanges'
import { useMintedMcpToken } from '../../../lib/mcpMintedToken'
import { useAsync } from '../../../lib/useAsync'
import { useLoadedSettings } from '../cache'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'mcp-connect'
export const area: AreaId = 'mcp'
export const title = 'Connect snippet'
export const order = 40
export const hideWhenEmpty = true

/** Read in a child, so nothing is asked of the agent while the assistant is unavailable. */
function Snippet() {
  const settings = useLoadedSettings()
  // The mode as Auth mode last saved it (#1921).
  const authVersion = useMcpAuthVersion()
  const auth = useAsync(() => api.getMcpAuth(), [authVersion])
  const token = useMintedMcpToken()
  return <McpConnectSnippet publicUrl={settings.public_url} authMode={auth.data?.mode ?? null} token={token ?? undefined} />
}

/** The commands that connect an MCP client; shown only where the assistant is available (#251). */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <Snippet />}</Section>
}
