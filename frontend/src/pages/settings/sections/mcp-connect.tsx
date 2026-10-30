import { useRef } from 'react'
import { useAiAvailability } from '../../../agent/chat/availability'
import { api } from '../../../api/client'
import type { McpAuthMode } from '../../../api/mcpTokens'
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
  // A failed read falls back to the mode the token list carries, as the Tokens card does.
  const failed = auth.error !== undefined
  const fallback = useAsync(
    () => (failed ? api.listMcpTokens().then((list) => list.auth_mode) : Promise.resolve(null)),
    [authVersion, failed],
  )
  const token = useMintedMcpToken()
  // The last mode read, kept while a new read is under way, so the snippet never goes
  // and comes back when Auth mode saves (#781).
  const known = useRef<{ mode: McpAuthMode | null } | undefined>(undefined)
  if (auth.data) known.current = { mode: auth.data.mode }
  else if (failed && fallback.data !== undefined) known.current = { mode: fallback.data }
  // Nothing until the mode is first known, so a header never shows and then goes (#781).
  if (!known.current) return null
  return (
    <McpConnectSnippet
      publicUrl={settings.public_url}
      authMode={known.current.mode}
      token={token ?? undefined}
    />
  )
}

/** The commands that connect an MCP client; shown only where the assistant is available (#251). */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <Snippet />}</Section>
}
