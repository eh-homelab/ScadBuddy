import type { Diagnostic } from '../api/types'

/** The owner string every marker this app sets carries, so it can replace its own. */
export const MARKER_OWNER = 'openscad-check'

/**
 * `monaco.MarkerSeverity` as plain numbers — Hint 1, Info 2, Warning 4, Error 8.
 * Spelled out so this module (and its test) pull no editor in.
 */
const SEVERITY: Record<Diagnostic['severity'], number> = { error: 8, warning: 4, trace: 2 }

export interface EditorMarker {
  severity: number
  message: string
  startLineNumber: number
  startColumn: number
  endLineNumber: number
  endColumn: number
  source: string
}

/**
 * OpenSCAD reports a line and no column, so a marker spans the line's text: from its
 * first non-space character to its end. A diagnostic that names no line at all — a
 * schema-derivation failure, say — lands on line 1 rather than being dropped.
 */
export function toMarkers(diagnostics: Diagnostic[], source: string): EditorMarker[] {
  const lines = source.split('\n')
  return diagnostics.map((diagnostic) => {
    const line =
      diagnostic.line != null && diagnostic.line >= 1 && diagnostic.line <= lines.length
        ? diagnostic.line
        : 1
    const text = lines[line - 1] ?? ''
    const startColumn = text.length - text.trimStart().length + 1
    return {
      severity: SEVERITY[diagnostic.severity] ?? SEVERITY.error,
      message: diagnostic.message,
      startLineNumber: line,
      startColumn,
      endLineNumber: line,
      endColumn: Math.max(text.length + 1, startColumn + 1),
      source: 'OpenSCAD',
    }
  })
}
