import Editor from '@monaco-editor/react'
import type * as Monaco from 'monaco-editor/editor/editor.api'
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import type { Diagnostic } from '../api/types'
import { connectLanguageServer } from '../lib/languageClient'
import { MARKER_OWNER, toMarkers } from '../lib/markers'
import { OPENSCAD_LANGUAGE_ID, monaco, setupMonaco } from '../lib/monaco'
import { SCADBUDDY_DARK, SCADBUDDY_LIGHT } from '../lib/openscadLanguage'

setupMonaco()

/**
 * #254 — how the agent's `replace_range` reaches the editor: as one Monaco edit between
 * undo stops, so Ctrl+Z takes it back exactly as it would a paste, and `onChange` runs
 * as it does for typing.
 */
export interface SourceEditHandle {
  replace: (
    range: { startLineNumber: number; startColumn: number; endLineNumber: number; endColumn: number },
    text: string,
  ) => void
  /** The editor's own element, for the highlight. */
  element: () => HTMLElement | null
}

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
  /** The openscad-lsp WebSocket path; without one the editor has no completion or hover. */
  languageServer?: string
  label: string
  readOnly?: boolean
  /** Filled in once the editor mounts; see `SourceEditHandle`. */
  editRef?: RefObject<SourceEditHandle | null>
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

const READ_ONLY_OPTIONS = { ...OPTIONS, readOnly: true }

/**
 * The one place that knows an editor is involved. Its props are `value`/`onChange`/
 * `errors`, so swapping the implementation — or bolting a language client onto it —
 * touches nothing else.
 */
export function SourceEditor({
  value,
  onChange,
  errors = [],
  uri,
  languageServer,
  label,
  readOnly = false,
  editRef,
}: Props) {
  const editor = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null)
  const [textModel, setTextModel] = useState<Monaco.editor.ITextModel | null>(null)

  const applyMarkers = useCallback(() => {
    const model = editor.current?.getModel()
    if (!model) return
    monaco.editor.setModelMarkers(model, MARKER_OWNER, toMarkers(errors, model.getValue()))
  }, [errors])

  useEffect(applyMarkers, [applyMarkers])

  useEffect(
    () => () => {
      if (editRef) editRef.current = null
    },
    [editRef],
  )

  // `path` puts @monaco-editor/react in multi-model mode: it creates a text model per
  // URI and leaves the lifecycle to us. Without this, every model opened in a session
  // keeps its content, undo stack and tokenizer state alive for the life of the tab.
  useEffect(
    () => () => {
      monaco.editor.getModel(monaco.Uri.parse(uri))?.dispose()
    },
    [uri],
  )

  // One server session per open model: a new URI is a new document, and the old
  // session goes with the model it was about.
  useEffect(() => {
    if (!textModel || !languageServer) return
    const client = connectLanguageServer(textModel, languageServer)
    return () => client.dispose()
  }, [textModel, languageServer])

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
        if (editRef) {
          editRef.current = {
            replace: (range, text) => {
              instance.pushUndoStop()
              instance.executeEdits('scadbuddy-agent', [{ range, text, forceMoveMarkers: true }])
              instance.pushUndoStop()
              instance.revealRangeInCenterIfOutsideViewport(range)
            },
            element: () => instance.getDomNode(),
          }
        }
        instance.updateOptions({ ariaLabel: label })
        applyMarkers()
        setTextModel(instance.getModel())
        instance.onDidChangeModel(() => setTextModel(instance.getModel()))
      }}
      options={readOnly ? READ_ONLY_OPTIONS : OPTIONS}
      loading={<span className="text-[13px] text-muted">Loading the editor</span>}
    />
  )
}
