import Editor from '@monaco-editor/react'
import type * as Monaco from 'monaco-editor/editor/editor.api'
import { LEAVE_HINT, letTabLeave } from '../../lib/editorExit'
import { monaco, setupMonaco } from '../../lib/monaco'
import { editorTheme } from '../../lib/openscadLanguage'
import { useLatest } from '../../lib/useLatest'
import { Skeleton } from '@/components/ui/skeleton'
import { languageOf } from './hostedDraft'

setupMonaco()

const OPTIONS: Monaco.editor.IStandaloneEditorConstructionOptions = {
  minimap: { enabled: false },
  fontSize: 13,
  tabSize: 2,
  insertSpaces: true,
  automaticLayout: true,
  scrollBeyondLastLine: false,
  wordWrap: 'on',
  renderWhitespace: 'selection',
  padding: { top: 8, bottom: 8 },
}

interface Props {
  plugin: string
  path: string
  /** The text it opens with; the editor owns the text from then on and reports each change. */
  initialValue: string
  onChange: (next: string) => void
  /** Ctrl/Cmd+S: the pane's Save. */
  onSave: () => void
}

/**
 * #6937 — one hosted plugin draft file in Monaco (the editor the template and flow pages use,
 * `lib/monaco.ts`), a lazy chunk of its own so Settings loads it only when a file opens. The
 * caller keys it by path: a file's model is dropped when another opens (`keepCurrentModel`
 * false), so a deleted or renamed path never comes back with old text.
 */
export default function HostedFileEditor({ plugin, path, initialValue, onChange, onSave }: Props) {
  const latest = useLatest({ onSave })
  return (
    <div className="flex h-[26rem] flex-col overflow-hidden rounded-lg bg-card ring-1 ring-foreground/10">
      <div className="min-h-0 flex-1">
        <Editor
          path={`file:///hosted-plugins/${plugin}/${path}`}
          language={languageOf(path)}
          theme={editorTheme()}
          defaultValue={initialValue}
          onChange={(next) => onChange(next ?? '')}
          onMount={(instance) => {
            letTabLeave(instance, `Draft file ${path}`)
            instance.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => latest.current.onSave())
          }}
          options={OPTIONS}
          loading={<Skeleton role="status" aria-label="Loading the editor" className="size-full rounded-none" />}
        />
      </div>
      <p aria-hidden="true" className="shrink-0 border-t px-3 py-0.5 text-[11px] text-muted-foreground">
        {LEAVE_HINT}. Ctrl+S saves.
      </p>
    </div>
  )
}
