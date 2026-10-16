import Editor from '@monaco-editor/react'
import type * as Monaco from 'monaco-editor/editor/editor.api'
import { useEffect, useRef } from 'react'
import type { FlowHostInterface, FlowScriptProblem } from '../../api/types'
import { LEAVE_HINT, letTabLeave } from '../../lib/editorExit'
import { FLOW_MARKER_OWNER, flowCompletions, flowHover, problemMarkers } from '../../lib/flowLanguage'
import { monaco, PYTHON_LANGUAGE_ID, setupMonaco } from '../../lib/monaco'
import { editorTheme } from '../../lib/openscadLanguage'
import { useLatest } from '../../lib/useLatest'
import { Skeleton } from '@/components/ui/skeleton'

setupMonaco()

/** Where a flow version's script lives in Monaco: what the providers below answer for. */
const flowUri = (version: string, name: string) => `file:///flows/${version}/${name}/flow.py`
const isFlow = (model: Monaco.editor.ITextModel) => model.uri.path.startsWith('/flows/')

/** What completion and hover read: the open editor's host functions. One editor at a time. */
let help: FlowHostInterface | null = null
let registered = false

function registerProviders(): void {
  if (registered) return
  registered = true
  const KIND = {
    function: monaco.languages.CompletionItemKind.Function,
    type: monaco.languages.CompletionItemKind.Struct,
    tool: monaco.languages.CompletionItemKind.Value,
  }
  monaco.languages.registerCompletionItemProvider(PYTHON_LANGUAGE_ID, {
    triggerCharacters: ['"', "'"],
    provideCompletionItems(model, position) {
      if (!help || !isFlow(model)) return { suggestions: [] }
      const before = model.getLineContent(position.lineNumber).slice(0, position.column - 1)
      const word = model.getWordUntilPosition(position)
      const range = {
        startLineNumber: position.lineNumber,
        endLineNumber: position.lineNumber,
        startColumn: word.startColumn,
        endColumn: word.endColumn,
      }
      return {
        suggestions: flowCompletions(before, help).map((item) => ({
          label: item.label,
          kind: KIND[item.kind],
          detail: item.detail,
          documentation: { value: item.documentation },
          insertText: item.insertText,
          insertTextRules: item.snippet ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
          range,
        })),
      }
    },
  })
  monaco.languages.registerHoverProvider(PYTHON_LANGUAGE_ID, {
    provideHover(model, position) {
      if (!help || !isFlow(model)) return null
      const word = model.getWordAtPosition(position)
      if (!word) return null
      const contents = flowHover(word.word, model.getLineContent(position.lineNumber), help)
      if (!contents) return null
      return {
        range: new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn),
        contents: contents.map((value) => ({ value })),
      }
    },
  })
}

const OPTIONS: Monaco.editor.IStandaloneEditorConstructionOptions = {
  minimap: { enabled: false },
  fontSize: 13,
  tabSize: 4,
  insertSpaces: true,
  automaticLayout: true,
  scrollBeyondLastLine: false,
  renderWhitespace: 'selection',
  padding: { top: 8, bottom: 8 },
}

interface Props {
  /** The version being edited: a new one is a new text model. */
  version: string
  name: string
  /**
   * The text the editor starts with. The editor owns the text from then on and reports
   * each change: a controlled value, written back on each render, could land after a
   * faster keystroke and undo it.
   */
  initialValue: string
  onChange: (next: string) => void
  /** The type check's problems; each marks its line. */
  problems: FlowScriptProblem[]
  /** The host functions and tools to complete; without them, Python only. */
  hostFunctions?: FlowHostInterface
  /** What Ctrl/Cmd+S does: the page's Save. */
  onSave: () => void
  label: string
}

/**
 * #2487 — a flow's script in Monaco, in Python mode (the source page's lazy chunk,
 * `lib/monaco.ts`), with the type check's problems as markers and completion and hover
 * for the host functions and the manifest's tools.
 */
export function FlowEditor({ version, name, initialValue, onChange, problems, hostFunctions, onSave, label }: Props) {
  const editor = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null)
  const uri = flowUri(version, name)
  const latest = useLatest({ onSave })

  useEffect(() => {
    registerProviders()
    help = hostFunctions ?? null
  }, [hostFunctions])

  useEffect(() => {
    const model = editor.current?.getModel()
    if (model) monaco.editor.setModelMarkers(model, FLOW_MARKER_OWNER, problemMarkers(problems, model.getValue()))
  }, [problems])

  return (
    <div className="flex h-[28rem] flex-col overflow-hidden rounded-xl bg-card ring-1 ring-foreground/10">
      <div className="min-h-0 flex-1">
        <Editor
          path={uri}
          language={PYTHON_LANGUAGE_ID}
          theme={editorTheme()}
          defaultValue={initialValue}
          onChange={(next) => onChange(next ?? '')}
          onMount={(instance) => {
            editor.current = instance
            letTabLeave(instance, label)
            instance.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => latest.current.onSave())
            const model = instance.getModel()
            if (model) monaco.editor.setModelMarkers(model, FLOW_MARKER_OWNER, problemMarkers(problems, model.getValue()))
          }}
          options={OPTIONS}
          loading={<Skeleton role="status" aria-label="Loading the editor" className="size-full rounded-none" />}
        />
      </div>
      <p aria-hidden="true" className="shrink-0 border-t px-3 py-0.5 text-[11px] text-muted-foreground">
        {LEAVE_HINT}
      </p>
    </div>
  )
}
