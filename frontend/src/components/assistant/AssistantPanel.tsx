import { loadChatTransportFactory, type ChatTransportFactory } from '../../agent/chat/transport'
import { useAsync } from '../../lib/useAsync'
import { McpAuthBanner } from '../McpAuthBanner'
import { AssistantChat, type OpenRequest } from './AssistantChat'

interface Props {
  onClose: () => void
  focusKey: number
  /** Inside Bambuddy's iframe (`AppShell`'s `embedded`). */
  embedded?: boolean
  /** Tests pass one; the app loads whichever this build has. */
  factory?: ChatTransportFactory
  /** #931 — a session a page asked to open (AppShell's AssistantOpener). */
  openRequest?: OpenRequest | null
  onOpenHandled?: () => void
}

export function AssistantPanel({ onClose, focusKey, factory, embedded, openRequest, onOpenHandled }: Props) {
  const loaded = useAsync(async () => factory ?? (await loadChatTransportFactory()), [factory])
  if (loaded.loading) {
    return <p className="p-3 text-[12.5px] text-muted">Connecting to the assistant…</p>
  }
  if (!loaded.data) {
    return (
      <p role="alert" className="p-3 text-[12.5px] text-warn">
        The assistant isn&apos;t reachable in this build.
      </p>
    )
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* #1921 — while /mcp takes calls without a token, the panel says so too. */}
      <McpAuthBanner link className="m-2 shrink-0" />
      <div className="min-h-0 flex-1">
        <AssistantChat
          factory={loaded.data}
          onClose={onClose}
          focusKey={focusKey}
          embedded={embedded}
          openRequest={openRequest}
          onOpenHandled={onOpenHandled}
        />
      </div>
    </div>
  )
}
