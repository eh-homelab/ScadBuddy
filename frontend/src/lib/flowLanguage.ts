import type { FlowHostInterface, FlowScriptProblem } from '../api/types'
import type { EditorMarker } from './markers'

/**
 * #2487 — what the flow editor knows about a script's language beyond Python: its
 * problems as markers, and completion and hover over the host functions and the tool
 * manifest (`GET /workflow-script/host-functions`, rendered from the same host functions
 * the type check uses). No editor is imported here, so it is tested without one.
 */

export const FLOW_MARKER_OWNER = 'flow-check'

/** `monaco.MarkerSeverity.Error`. */
const ERROR = 8

/** A problem with no line, or one past the end, marks line 1 rather than vanishing. */
export function problemMarkers(problems: FlowScriptProblem[], source: string): EditorMarker[] {
  const lines = source.split('\n')
  return problems.map((problem) => {
    const line = problem.line != null && problem.line >= 1 && problem.line <= lines.length ? problem.line : 1
    const text = lines[line - 1] ?? ''
    const startColumn = text.length - text.trimStart().length + 1
    return {
      severity: ERROR,
      message: problem.message,
      startLineNumber: line,
      startColumn,
      endLineNumber: line,
      endColumn: Math.max(text.length + 1, startColumn + 1),
      source: 'Type check',
    }
  })
}

export interface FlowCompletion {
  label: string
  kind: 'function' | 'type' | 'tool'
  detail: string
  documentation: string
  insertText: string
  /** `insertText` is a snippet, with a tab stop per parameter. */
  snippet: boolean
}

/** The cursor is in the name argument of `tool("…` or `tool_result("…`. */
const TOOL_NAME_ARG = /\btool(?:_result)?\(\s*["'][\w-]*$/
/** In a string or a comment, where a host function's name means nothing. */
function inStringOrComment(before: string): boolean {
  let quote: string | null = null
  for (let i = 0; i < before.length; i++) {
    const ch = before[i]
    if (quote) {
      if (ch === '\\') i++
      else if (ch === quote) quote = null
    } else if (ch === '#') return true
    else if (ch === '"' || ch === "'") quote = ch
  }
  return quote !== null
}

/** What completes at the cursor, given the line's text before it. */
export function flowCompletions(before: string, help: FlowHostInterface): FlowCompletion[] {
  if (TOOL_NAME_ARG.test(before)) {
    return help.tools.map((tool) => ({
      label: tool.name,
      kind: 'tool',
      detail: `${tool.tier} tool`,
      documentation: toolDoc(tool.name, tool.tier),
      insertText: tool.name,
      snippet: false,
    }))
  }
  if (inStringOrComment(before)) return []
  return [
    ...help.functions.map((fn) => ({
      label: fn.name,
      kind: 'function' as const,
      detail: fn.signature,
      documentation: fn.doc,
      insertText: `${fn.name}(${fn.parameters.map((p, i) => `\${${i + 1}:${p}}`).join(', ')})`,
      snippet: true,
    })),
    ...help.types.map((type) => ({
      label: type.name,
      kind: 'type' as const,
      detail: 'result type',
      documentation: '```python\n' + type.source + '\n```',
      insertText: type.name,
      snippet: false,
    })),
  ]
}

function toolDoc(name: string, tier: string): string {
  const waits = tier === 'outward' ? ' It waits for a person to approve each call.' : ''
  return `ScadBuddy's tool \`${name}\` (${tier}).${waits}`
}

/** The hover for `word`, as Markdown paragraphs; null when it names nothing known. */
export function flowHover(word: string, line: string, help: FlowHostInterface): string[] | null {
  const fn = help.functions.find((f) => f.name === word)
  if (fn) return ['```python\n' + fn.signature + '\n```', fn.doc]
  const type = help.types.find((t) => t.name === word)
  if (type) return ['```python\n' + type.source + '\n```']
  const tool = help.tools.find((t) => t.name === word)
  if (tool && /\btool(?:_result)?\(/.test(line)) return [toolDoc(tool.name, tool.tier)]
  return null
}
