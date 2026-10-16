import type { Origin, Owner, SessionStatus } from '../../agent/chat/protocol'

/**
 * #2372 — what a reload (or a crashed tab) brings back: whether the assistant panel was
 * open, and the chat it showed. Per tab, in sessionStorage, so another tab keeps its own;
 * a duplicated tab showing the same chat is fine. Storage that throws (private mode,
 * blocked site data) remembers nothing.
 */
const OPEN_KEY = 'scadbuddy.assistant.open'
const ACTIVE_KEY = 'scadbuddy.assistant.session'

function read(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, value)
  } catch {
    // Nothing is remembered; the panel still works for this page.
  }
}

export const panelWasOpen = (): boolean => read(OPEN_KEY) === '1'
export const rememberPanelOpen = (open: boolean): void => write(OPEN_KEY, open ? '1' : null)
export const rememberedSession = (): string | null => read(ACTIVE_KEY)
export const rememberSession = (id: string | null): void => write(ACTIVE_KEY, id)

const UNDER_WAY: readonly SessionStatus[] = ['running', 'waiting_input', 'waiting_approval']

/**
 * Whether this tab is coming back from a reload: it remembers a chat or an open panel.
 * A tab duplicated from this one, or opened by it, starts with a copy of its
 * sessionStorage, so it counts too and picks up the same chat, as a reload would.
 */
export const reloaded = (): boolean => rememberedSession() !== null || panelWasOpen()

/**
 * The chat to show once the list has come in: the one this tab last showed, if it is
 * still listed, else the newest of the viewer's own chats (not an MCP client's or an
 * analyzer's session, nor another owner's, #3164) that is running or waiting on the user.
 * `order` is newest first.
 */
export function sessionToRestore(
  remembered: string | null,
  order: readonly string[],
  sessions: Readonly<
    Record<string, { status: SessionStatus; origin: Origin; owner: Pick<Owner, 'kind'> } | undefined>
  >,
): string | null {
  if (remembered && sessions[remembered]) return remembered
  return (
    order.find((id) => {
      const s = sessions[id]
      // Any browser owner is "you", as in OwnerBadge: a deployment has one browser principal.
      return s?.origin === 'chat' && s.owner.kind === 'browser' && UNDER_WAY.includes(s.status)
    }) ?? null
  )
}

/**
 * The chat a panel opens on once the list is in. A reloaded tab gets `sessionToRestore`'s
 * answer. A tab that was not reloaded keeps only the chat it showed: its panel was
 * remounted on the same page (the assistant switched off and on), and falling back to
 * a chat under way there is what a new tab must not do (#3164, #3249 review).
 */
export function chatToRestore(
  reloaded: boolean,
  remembered: string | null,
  order: readonly string[],
  sessions: Parameters<typeof sessionToRestore>[2],
): string | null {
  if (reloaded) return sessionToRestore(remembered, order, sessions)
  return remembered && sessions[remembered] ? remembered : null
}
