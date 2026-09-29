import Editor from '@monaco-editor/react'
import type * as Monaco from 'monaco-editor/editor/editor.api'
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import type { Diagnostic } from '../api/types'
import { connectLanguageServer } from '../lib/languageClient'
import { definitionFile, definitionLabel, directoryOf, type DefinitionFile } from '../lib/lsp'
import { MARKER_OWNER, toMarkers } from '../lib/markers'
import { OPENSCAD_LANGUAGE_ID, monaco, setupMonaco } from '../lib/monaco'
import { SCADBUDDY_DARK, SCADBUDDY_LIGHT } from '../lib/openscadLanguage'
import { useLatest } from '../lib/useLatest'
import { Button } from './ui/Button'

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
  /**
   * #185 — reads a file a definition lands in outside the model's source, so
   * go-to-definition can open it read-only. Without it, only this file's are followed.
   */
  readFile?: (file: DefinitionFile) => Promise<string>
  label: string
  readOnly?: boolean
  /** Filled in once the editor mounts; see `SourceEditHandle`. */
  editRef?: RefObject<SourceEditHandle | null>
}

/** A definition's file the editor is showing in place of the model's source. */
interface Viewing {
  /** The model URI it was opened from: a new model is never shown someone else's. */
  from: string
  uri: string
  label: string
}

type Target = Monaco.IRange | Monaco.IPosition

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
const VIEWING_OPTIONS = {
  ...READ_ONLY_OPTIONS,
  readOnlyMessage: { value: 'This file is read-only here. Go back to edit the model.' },
}

function reveal(instance: Monaco.editor.ICodeEditor, target: Target) {
  if ('startLineNumber' in target) {
    instance.setSelection(target)
    instance.revealRangeInCenterIfOutsideViewport(target)
  } else {
    instance.setPosition(target)
    instance.revealPositionInCenterIfOutsideViewport(target)
  }
}

/**
 * The one place that knows an editor is involved. Its props are `value`/`onChange`/
 * `errors`, so swapping the implementation — or bolting a language client onto it —
 * touches nothing else.
 *
 * #185 — go-to-definition into another file (`readFile`) shows that file in this same
 * editor, read-only, under a bar that names it and leads back to the model. The
 * model's own text model is kept as it was, undo stack and all, and gets the value,
 * the markers and the language server throughout.
 */
export function SourceEditor({
  value,
  onChange,
  errors = [],
  uri,
  languageServer,
  readFile,
  label,
  readOnly = false,
  editRef,
}: Props) {
  const editor = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null)
  const [textModel, setTextModel] = useState<Monaco.editor.ITextModel | null>(null)
  const [viewing, setViewing] = useState<Viewing | null>(null)
  // Where to put the cursor once the editor shows the model a jump switched to.
  const landing = useRef<Target | null>(null)
  const latest = useLatest({ uri, onChange })
  // A new model ends any definition view, rather than hiding it until that model comes
  // back: by then the file's model has gone with the old session, and the view would
  // reopen on an empty one. Reset while rendering, React's way to follow a prop.
  const [viewingFor, setViewingFor] = useState(uri)
  if (viewingFor !== uri) {
    setViewingFor(uri)
    setViewing(null)
  }
  const shown = viewing?.from === uri ? viewing : null

  const ownModel = useCallback(() => monaco.editor.getModel(monaco.Uri.parse(uri)), [uri])

  const applyMarkers = useCallback(() => {
    const model = editor.current ? ownModel() : null
    if (!model) return
    monaco.editor.setModelMarkers(model, MARKER_OWNER, toMarkers(errors, model.getValue()))
  }, [errors, ownModel])

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
      // A jump into the old model's definitions is not one into the new model's.
      landing.current = null
    },
    [uri],
  )

  // One server session per open model: a new URI is a new document, and the old
  // session goes with the model it was about.
  useEffect(() => {
    if (!textModel || !languageServer) return
    const client = connectLanguageServer(textModel, languageServer, readFile)
    return () => client.dispose()
  }, [textModel, languageServer, readFile])

  // Go-to-definition into a file the language client opened, and back: Monaco asks
  // this whenever a jump leaves the model the editor shows.
  useEffect(() => {
    const opener = monaco.editor.registerEditorOpener({
      openCodeEditor(source, resource, selectionOrPosition) {
        if (source !== editor.current) return false
        const from = latest.current.uri
        // Both as Monaco spells them (`builtin:x` is `builtin%3Ax`), as the client does.
        const own = monaco.Uri.parse(from).toString()
        const target = resource.toString()
        landing.current = selectionOrPosition ?? null
        if (target === own) {
          if (source.getModel()?.uri.toString() === own) {
            // Already showing the model ("Open Definition to the Side" in a single
            // editor): no switch will run the landing effect, so move the cursor here.
            landing.current = null
            if (selectionOrPosition) reveal(source, selectionOrPosition)
          } else {
            setViewing(null)
          }
          return true
        }
        const file = definitionFile(target, directoryOf(own))
        if (!file || !monaco.editor.getModel(resource)) return false
        setViewing({ from, uri: target, label: definitionLabel(file) })
        return true
      },
    })
    return () => opener.dispose()
  }, [latest])

  // After @monaco-editor/react has switched models for `path` (a child's effect, so
  // it has run by now): the cursor goes where the jump pointed.
  useEffect(() => {
    const at = landing.current
    landing.current = null
    if (at && editor.current) reveal(editor.current, at)
  }, [shown])

  const back = () => {
    landing.current = null
    setViewing(null)
  }

  const dark =
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
      : true

  const fileName = uri.slice(uri.lastIndexOf('/') + 1)

  return (
    <div className="flex h-full min-h-0 flex-col">
      {shown && (
        <div
          data-testid="definition-bar"
          className="flex shrink-0 items-center justify-between gap-3 border-b border-line bg-surface-2 px-3 py-1"
        >
          <span className="min-w-0 truncate text-[12px] text-muted">
            <span className="sb-num text-ink">{shown.label}</span> — read-only
          </span>
          <Button size="sm" variant="ghost" onClick={back}>
            ← Back to {fileName}
          </Button>
        </div>
      )}
      <div className="min-h-0 flex-1">
        <Editor
          path={shown?.uri ?? uri}
          language={OPENSCAD_LANGUAGE_ID}
          theme={dark ? SCADBUDDY_DARK : SCADBUDDY_LIGHT}
          // Held back while a definition is shown: the wrapper writes `value` into
          // whichever model the editor has, and that is not the model's source then.
          value={shown ? undefined : value}
          onChange={(next) => onChange(next ?? '')}
          onMount={(instance) => {
            editor.current = instance
            if (editRef) {
              editRef.current = {
                replace: (range, text) => {
                  const own = monaco.editor.getModel(monaco.Uri.parse(latest.current.uri))
                  if (own && instance.getModel() !== own) {
                    // Showing a definition: the edit is still the model's, one undo
                    // step on its own stack, and the editor goes back to show it.
                    own.pushStackElement()
                    own.pushEditOperations([], [{ range, text, forceMoveMarkers: true }], () => null)
                    own.pushStackElement()
                    latest.current.onChange(own.getValue())
                    landing.current = range
                    setViewing(null)
                    return
                  }
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
            // Only the model's own source: a definition shown read-only has no session.
            instance.onDidChangeModel(() => {
              const next = instance.getModel()
              if (next && next.uri.toString() === monaco.Uri.parse(latest.current.uri).toString()) {
                setTextModel(next)
              }
            })
          }}
          options={shown ? VIEWING_OPTIONS : readOnly ? READ_ONLY_OPTIONS : OPTIONS}
          loading={<span className="text-[13px] text-muted">Loading the editor</span>}
        />
      </div>
    </div>
  )
}
