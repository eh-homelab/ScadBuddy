import type { Origin, SessionStatus } from '../../agent/chat/protocol'

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
 * The chat to show once the list has come in: the one this tab last showed, if it is
 * still listed, else the newest chat (not an MCP client's or an analyzer's session) that
 * is running or waiting on the user. `order` is newest first.
 */
export function sessionToRestore(
  remembered: string | null,
  order: readonly string[],
  sessions: Readonly<Record<string, { status: SessionStatus; origin: Origin } | undefined>>,
): string | null {
  if (remembered && sessions[remembered]) return remembered
  return (
    order.find((id) => {
      const s = sessions[id]
      return s?.origin === 'chat' && UNDER_WAY.includes(s.status)
    }) ?? null
  )
}
