import type * as Monaco from 'monaco-editor/editor/editor.api'
import { monaco } from './monaco'

/** #997 — how to leave the editor by keyboard, since Tab indents in it. */
export const LEAVE_HINT = 'Esc, then Tab, to leave the editor'

/**
 * #997 — Tab indents, so on its own it can never leave the editor (WCAG 2.1.2).
 * Escape arms Monaco's tab-focus mode for the next key: Tab or Shift+Tab then moves
 * focus on, and any other key, or leaving, puts Tab back to indenting. Only with no
 * widget open and nothing selected: Escape still closes completion, find and the rest,
 * collapses a selection and removes secondary cursors. #1672 — the way out is part of
 * the editor's name, so a screen reader says it on focus.
 */
export function letTabLeave(instance: Monaco.editor.IStandaloneCodeEditor, label: string): void {
  instance.addCommand(
    monaco.KeyCode.Escape,
    () => instance.updateOptions({ tabFocusMode: true }),
    '!suggestWidgetVisible && !findWidgetVisible && !parameterHintsVisible && ' +
      '!renameInputVisible && !referenceSearchVisible && !inSnippetMode && !editorHasMultipleSelections && ' +
      '!editorHasSelection',
  )
  const passing = new Set([
    monaco.KeyCode.Escape,
    monaco.KeyCode.Tab,
    monaco.KeyCode.Shift,
    monaco.KeyCode.Ctrl,
    monaco.KeyCode.Alt,
    monaco.KeyCode.Meta,
  ])
  instance.onKeyDown((event) => {
    if (!passing.has(event.keyCode)) instance.updateOptions({ tabFocusMode: false })
  })
  instance.onDidBlurEditorText(() => instance.updateOptions({ tabFocusMode: false }))
  instance.updateOptions({ ariaLabel: `${label}. ${LEAVE_HINT}` })
}
