import Editor from '@monaco-editor/react'
import type * as Monaco from 'monaco-editor/editor/editor.api'
import { useCallback, useEffect, useRef } from 'react'
import type { Diagnostic } from '../api/types'
import { MARKER_OWNER, toMarkers } from '../lib/markers'
import { OPENSCAD_LANGUAGE_ID, monaco, setupMonaco } from '../lib/monaco'
import { SCADBUDDY_DARK, SCADBUDDY_LIGHT } from '../lib/openscadLanguage'

setupMonaco()

interface Props {
  value: string
  onChange: (next: string) => void
  /** OpenSCAD's own diagnostics; they become editor markers, squiggles and hovers. */
  errors?: Diagnostic[]
  /**
   * The model's URI, e.g. `file:///models/name-keychain/model.scad`. A real file URI
   * rather than an anonymous buffer so a language server (#95) has something to name.
   */
  uri: string
  label: string
}

const OPTIONS: Monaco.editor.IStandaloneEditorConstructionOptions = {
  minimap: { enabled: false },
  fontSize: 13,
  tabSize: 2,
  insertSpaces: true,
  automaticLayout: true,
  scrollBeyondLastLine: false,
  smoothScrolling: true,
  renderWhitespace: 'selection',
  wordWrap: 'on',
  padding: { top: 8, bottom: 8 },
}

/**
 * The one place that knows an editor is involved. Its props are `value`/`onChange`/
 * `errors`, so swapping the implementation — or bolting a language client onto it —
 * touches nothing else.
 */
export function SourceEditor({ value, onChange, errors = [], uri, label }: Props) {
  const editor = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null)

  const applyMarkers = useCallback(() => {
    const model = editor.current?.getModel()
    if (!model) return
    monaco.editor.setModelMarkers(model, MARKER_OWNER, toMarkers(errors, model.getValue()))
  }, [errors])

  useEffect(applyMarkers, [applyMarkers])

  // `path` puts @monaco-editor/react in multi-model mode: it creates a text model per
  // URI and leaves the lifecycle to us. Without this, every model opened in a session
  // keeps its content, undo stack and tokenizer state alive for the life of the tab.
  useEffect(
    () => () => {
      monaco.editor.getModel(monaco.Uri.parse(uri))?.dispose()
    },
    [uri],
  )

  const dark =
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
      : true

  return (
    <Editor
      path={uri}
      language={OPENSCAD_LANGUAGE_ID}
      theme={dark ? SCADBUDDY_DARK : SCADBUDDY_LIGHT}
      value={value}
      onChange={(next) => onChange(next ?? '')}
      onMount={(instance) => {
        editor.current = instance
        instance.updateOptions({ ariaLabel: label })
        applyMarkers()
      }}
      options={OPTIONS}
      loading={<span className="text-[13px] text-muted">Loading the editor</span>}
    />
  )
}
