import { useState } from 'react'
import { useAiAvailability } from '../../../agent/chat/availability'
import { api } from '../../../api/client'
import type { McpAuthMode } from '../../../api/mcpTokens'
import { McpConnectSnippet } from '../../../components/McpConnectSnippet'
import { useMcpAuthVersion } from '../../../lib/mcpAuthChanges'
import { tokensCardMounted, useMintedMcpToken, useTokensCard } from '../../../lib/mcpMintedToken'
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
  // The Tokens card's mode when it is on the page; this section reads its own only without it.
  const card = useTokensCard()
  const shared = card?.mode === undefined ? undefined : { mode: card.mode }
  // Its own read only without the card: with one whose mode is not known yet, wait for it.
  const own = card === undefined
  // `own` is the card's state at this render; `tokensCardMounted()` is the store's now, at
  // load time, which also covers a card whose layout effect runs in this same commit (#5021).
  const auth = useAsync(() => (own && !tokensCardMounted() ? api.getMcpAuth() : Promise.resolve(null)), [authVersion, own])
  // A failed read falls back to the mode the token list carries, as the Tokens card does.
  const failed = own && auth.error !== undefined
  const fallback = useAsync(
    () => (failed ? api.listMcpTokens().then((list) => list.auth_mode) : Promise.resolve(null)),
    [authVersion, failed],
  )
  const token = useMintedMcpToken()
  // The last mode read, kept while a new read is under way, so the snippet never goes
  // and comes back when Auth mode saves (#781).
  const [known, setKnown] = useState<{ mode: McpAuthMode | null } | undefined>(undefined)
  const read = shared
    ? shared
    : auth.data
    ? { mode: auth.data.mode }
    : failed && fallback.data !== undefined
      ? { mode: fallback.data }
      : undefined
  if (read && read.mode !== known?.mode) setKnown(read)
  // Nothing until the mode is first known, so a header never shows and then goes (#781).
  const shown = read ?? known
  if (!shown) return null
  return (
    <McpConnectSnippet
      publicUrl={settings.public_url}
      authMode={shown.mode}
      token={token ?? undefined}
    />
  )
}

/** The commands that connect an MCP client; shown only where the assistant is available (#251). */
export function Component() {
  const ai = useAiAvailability()
  return <Section id={id} title={title}>{ai.available && <Snippet />}</Section>
}
