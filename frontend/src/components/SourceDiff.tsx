import { DiffEditor } from '@monaco-editor/react'
import { OPENSCAD_LANGUAGE_ID, setupMonaco } from '../lib/monaco'
import { SCADBUDDY_DARK, SCADBUDDY_LIGHT } from '../lib/openscadLanguage'

setupMonaco()

/**
 * #1287 — two versions of a source side by side, read-only: the one saved elsewhere
 * (`original`) and the buffer (`modified`). Monaco's own diff editor, now that the
 * editor is in the bundle anyway (UnifiedDiff's note).
 */
export function SourceDiff({ original, modified }: { original: string; modified: string }) {
  const dark =
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
      : true
  return (
    <div data-testid="source-diff" className="h-80 border-t border-line">
      <DiffEditor
        original={original}
        modified={modified}
        language={OPENSCAD_LANGUAGE_ID}
        theme={dark ? SCADBUDDY_DARK : SCADBUDDY_LIGHT}
        options={{ readOnly: true, originalEditable: false, renderSideBySide: true, minimap: { enabled: false } }}
      />
    </div>
  )
}
