import { DiffEditor } from '@monaco-editor/react'
import { useEffect, useRef } from 'react'
import { OPENSCAD_LANGUAGE_ID, setupMonaco } from '../lib/monaco'
import { editorTheme } from '../lib/openscadLanguage'

setupMonaco()

type Disposable = { dispose(): void }

/**
 * #1287 — two versions of a source side by side, read-only: the one saved elsewhere
 * (`original`) and the buffer (`modified`). Monaco's own diff editor, now that the
 * editor is in the bundle anyway (UnifiedDiff's note).
 *
 * #2194 — on unmount `@monaco-editor/react` 4.7 disposes the two models *before* the
 * diff editor, and monaco's DiffEditorWidget reports that as an unexpected error
 * ("TextModel got disposed before DiffEditorWidget model got reset",
 * suren-atoyan/monaco-react#647). So the library is told to keep both models, and this
 * component disposes them itself once the library has disposed the editor.
 */
export function SourceDiff({ original, modified }: { original: string; modified: string }) {
  const models = useRef<Disposable[]>([])

  useEffect(
    () => () => {
      const owned = models.current
      models.current = []
      // After the library's own unmount, which disposes the editor that holds them.
      setTimeout(() => owned.forEach((model) => model.dispose()))
    },
    [],
  )

  return (
    <div data-testid="source-diff" className="h-80 border-t border-line">
      <DiffEditor
        original={original}
        modified={modified}
        language={OPENSCAD_LANGUAGE_ID}
        theme={editorTheme()}
        keepCurrentOriginalModel
        keepCurrentModifiedModel
        onMount={(editor) => {
          const model = editor.getModel()
          models.current = model ? [model.original, model.modified] : []
        }}
        options={{ readOnly: true, originalEditable: false, renderSideBySide: true, minimap: { enabled: false } }}
      />
    </div>
  )
}
