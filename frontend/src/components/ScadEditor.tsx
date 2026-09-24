import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { EditorState, StateEffect, StateField } from '@codemirror/state'
import {
  Decoration,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
  type DecorationSet,
} from '@codemirror/view'
import { tags } from '@lezer/highlight'
import { useEffect, useRef } from 'react'
import { openscadLanguage } from '../lib/scadMode'

/** Colours come from the app's own tokens so the editor is not a second theme. */
const highlight = HighlightStyle.define([
  { tag: tags.comment, color: 'var(--sb-faint)', fontStyle: 'italic' },
  { tag: tags.keyword, color: 'var(--sb-accent)' },
  { tag: tags.atom, color: 'var(--sb-accent)' },
  { tag: tags.number, color: 'var(--sb-ok)' },
  { tag: tags.string, color: 'var(--sb-ok)' },
  { tag: tags.operator, color: 'var(--sb-muted)' },
  { tag: tags.standard(tags.variableName), color: 'var(--sb-focus)' },
  { tag: tags.special(tags.variableName), color: 'var(--sb-focus)', fontStyle: 'italic' },
])

const theme = EditorView.theme({
  '&': { height: '100%', fontSize: '13px', backgroundColor: 'var(--sb-surface)' },
  '&.cm-focused': { outline: '2px solid var(--sb-focus)', outlineOffset: '-2px' },
  '.cm-scroller': { fontFamily: 'var(--font-mono, monospace)', lineHeight: '1.55' },
  '.cm-content': { caretColor: 'var(--sb-text)', color: 'var(--sb-text)' },
  '.cm-gutters': {
    backgroundColor: 'var(--sb-surface-2)',
    color: 'var(--sb-faint)',
    border: 'none',
    borderRight: '1px solid var(--sb-line)',
  },
  '.cm-activeLine': { backgroundColor: 'color-mix(in srgb, var(--sb-surface-2) 60%, transparent)' },
  '.cm-activeLineGutter': { backgroundColor: 'transparent', color: 'var(--sb-muted)' },
  '.cm-sb-error-line': {
    backgroundColor: 'color-mix(in srgb, var(--sb-warn) 18%, transparent)',
    boxShadow: 'inset 2px 0 0 var(--sb-warn)',
  },
})

const setErrorLines = StateEffect.define<number[]>()
const errorLine = Decoration.line({ class: 'cm-sb-error-line' })

const errorLineField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(decorations, transaction) {
    for (const effect of transaction.effects) {
      if (!effect.is(setErrorLines)) continue
      const doc = transaction.state.doc
      return Decoration.set(
        effect.value
          .filter((line) => line >= 1 && line <= doc.lines)
          .map((line) => errorLine.range(doc.line(line).from)),
        true,
      )
    }
    return decorations.map(transaction.changes)
  },
  provide: (field) => EditorView.decorations.from(field),
})

interface Props {
  value: string
  onChange: (next: string) => void
  /** 1-based lines OpenSCAD complained about; they get a marked background. */
  errorLines?: number[]
  label: string
}

/**
 * CodeMirror 6 with an OpenSCAD mode — line numbers, monospace, undo, tab-to-indent.
 * Deliberately not an IDE: no completion, no linting client-side. The only
 * authority on whether the source is valid is OpenSCAD itself, via the check API.
 */
export function ScadEditor({ value, onChange, errorLines = [], label }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView | null>(null)
  const notify = useRef(onChange)

  useEffect(() => {
    notify.current = onChange
  }, [onChange])

  useEffect(() => {
    if (!host.current) return
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          history(),
          highlightActiveLine(),
          highlightActiveLineGutter(),
          keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ 'aria-label': label, 'data-testid': 'scad-editor' }),
          openscadLanguage,
          syntaxHighlighting(highlight),
          errorLineField,
          theme,
          EditorView.updateListener.of((update) => {
            if (update.docChanged) notify.current(update.state.doc.toString())
          }),
        ],
      }),
    })
    view.current = editor
    return () => {
      editor.destroy()
      view.current = null
    }
    // Built once: `value` is pushed in by the effect below, and `label` never changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Only when the value was changed from outside (a reset, or the source arriving).
  useEffect(() => {
    const editor = view.current
    if (!editor || editor.state.doc.toString() === value) return
    editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } })
  }, [value])

  useEffect(() => {
    view.current?.dispatch({ effects: setErrorLines.of(errorLines) })
  }, [errorLines])

  return <div ref={host} className="h-full overflow-hidden" />
}
