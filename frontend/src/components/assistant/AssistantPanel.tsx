import { loadChatTransportFactory, type ChatTransportFactory } from '../../agent/chat/transport'
import { useAsync } from '../../lib/useAsync'
import { Skeleton } from '@/components/ui/skeleton'
import { McpAuthBanner } from '../McpAuthBanner'
import { AssistantChat, type OpenRequest } from './AssistantChat'
import { NotificationsOffer } from './NotificationsOffer'

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
  /** #3164 — whether this tab came back from a reload, read by the shell at page load. */
  reloaded?: boolean
}

export function AssistantPanel({ onClose, focusKey, factory, embedded, openRequest, onOpenHandled, reloaded }: Props) {
  const loaded = useAsync(async () => factory ?? (await loadChatTransportFactory()), [factory])
  if (loaded.loading) {
    return (
      <div role="status" className="flex flex-col gap-2 p-3">
        <span className="sr-only">Connecting to the assistant…</span>
        <Skeleton className="h-6 w-1/2" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-4/5" />
      </div>
    )
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
      {/* #815 — not in Bambuddy's frame, where the shell raises none (a cross-origin frame cannot ask). */}
      {!embedded && <NotificationsOffer className="shrink-0 border-b border-line px-3 py-2" />}
      <div className="min-h-0 flex-1">
        <AssistantChat
          factory={loaded.data}
          onClose={onClose}
          focusKey={focusKey}
          embedded={embedded}
          openRequest={openRequest}
          onOpenHandled={onOpenHandled}
          reloaded={reloaded}
        />
      </div>
    </div>
  )
}
