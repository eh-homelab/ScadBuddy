/**
 * Ctrl+` toggles the assistant, on every platform (Ctrl, not Cmd, on macOS too).
 *
 * Chosen to stay clear of what the app already binds: ScadBuddy's own only key
 * handler is Escape in `Dialog`, and the source editor is Monaco, whose defaults
 * claim most Ctrl/Cmd+letter and Ctrl+Shift+letter chords but not Ctrl+`.
 * Matched on `code` so it works on any keyboard layout.
 */
export const ASSISTANT_SHORTCUT_LABEL = 'Ctrl+`'
/** The `aria-keyshortcuts` spelling. */
export const ASSISTANT_SHORTCUT_ARIA = 'Control+`'

export function isAssistantShortcut(event: KeyboardEvent): boolean {
  return event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && event.code === 'Backquote'
}
