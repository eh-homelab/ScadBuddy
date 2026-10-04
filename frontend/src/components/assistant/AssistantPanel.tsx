import { loadChatTransportFactory, type ChatTransportFactory } from '../../agent/chat/transport'
import { useAsync } from '../../lib/useAsync'
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
}

export function AssistantPanel({ onClose, focusKey, factory, embedded, openRequest }: Props) {
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
    <AssistantChat
      factory={loaded.data}
      onClose={onClose}
      focusKey={focusKey}
      embedded={embedded}
      openRequest={openRequest}
    />
  )
}
