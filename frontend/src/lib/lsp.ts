/**
 * The Language Server Protocol shapes openscad-lsp answers with, and their Monaco
 * equivalents. Like `markers.ts`, this module pulls no editor in: Monaco's enums are
 * spelled out as numbers, so the mapping is testable without a DOM.
 */

export interface LspPosition {
  line: number
  character: number
}

export interface LspRange {
  start: LspPosition
  end: LspPosition
}

interface MarkupContent {
  kind: 'markdown' | 'plaintext'
  value: string
}

type MarkedString = string | { language: string; value: string }

export interface LspCompletionItem {
  label: string
  kind?: number
  detail?: string
  documentation?: string | MarkupContent
  insertText?: string
  insertTextFormat?: number
  textEdit?: { newText: string; range: LspRange }
  filterText?: string
  sortText?: string
}

export interface LspHover {
  contents: MarkupContent | MarkedString | MarkedString[]
  range?: LspRange
}

interface LspLocation {
  uri: string
  range: LspRange
}

interface LspLocationLink {
  targetUri: string
  targetRange: LspRange
  targetSelectionRange: LspRange
}

export interface LspTextEdit {
  newText: string
  range: LspRange
}

export interface MonacoRange {
  startLineNumber: number
  startColumn: number
  endLineNumber: number
  endColumn: number
}

/** `monaco.languages.CompletionItemKind` by LSP `CompletionItemKind`; the two differ. */
const COMPLETION_KIND: Record<number, number> = {
  1: 18, // Text
  2: 0, // Method
  3: 1, // Function
  4: 2, // Constructor
  5: 3, // Field
  6: 4, // Variable
  7: 5, // Class
  8: 7, // Interface
  9: 8, // Module
  10: 9, // Property
  11: 12, // Unit
  12: 13, // Value
  13: 15, // Enum
  14: 17, // Keyword
  15: 28, // Snippet
  16: 19, // Color
  17: 20, // File
  18: 21, // Reference
  19: 23, // Folder
  20: 16, // EnumMember
  21: 14, // Constant
  22: 6, // Struct
  23: 10, // Event
  24: 11, // Operator
  25: 24, // TypeParameter
}
const TEXT_KIND = 18
/** LSP `InsertTextFormat.Snippet`, and `CompletionItemInsertTextRule.InsertAsSnippet`. */
const SNIPPET_FORMAT = 2
const INSERT_AS_SNIPPET = 4

export function toRange(range: LspRange): MonacoRange {
  return {
    startLineNumber: range.start.line + 1,
    startColumn: range.start.character + 1,
    endLineNumber: range.end.line + 1,
    endColumn: range.end.character + 1,
  }
}

function toDocumentation(documentation: string | MarkupContent | undefined) {
  return typeof documentation === 'object' ? { value: documentation.value } : documentation
}

/** `word` is where the item lands when the server names no range of its own. */
export function toCompletion(item: LspCompletionItem, word: MonacoRange) {
  return {
    label: item.label,
    kind: COMPLETION_KIND[item.kind ?? 1] ?? TEXT_KIND,
    insertText: item.textEdit?.newText ?? item.insertText ?? item.label,
    insertTextRules: item.insertTextFormat === SNIPPET_FORMAT ? INSERT_AS_SNIPPET : 0,
    range: item.textEdit ? toRange(item.textEdit.range) : word,
    documentation: toDocumentation(item.documentation),
    detail: item.detail,
    filterText: item.filterText,
    sortText: item.sortText,
  }
}

function toMarkdown(content: MarkedString | MarkupContent) {
  if (typeof content === 'string') return { value: content }
  if ('language' in content) return { value: `\`\`\`${content.language}\n${content.value}\n\`\`\`` }
  return { value: content.value }
}

export function toHover(hover: LspHover | null) {
  if (!hover) return undefined
  const contents = Array.isArray(hover.contents) ? hover.contents : [hover.contents]
  return {
    contents: contents.map(toMarkdown),
    range: hover.range ? toRange(hover.range) : undefined,
  }
}

export function toLocations(result: LspLocation | (LspLocation | LspLocationLink)[] | null) {
  if (!result) return []
  return (Array.isArray(result) ? result : [result]).map((location) =>
    'targetUri' in location
      ? { uri: location.targetUri, range: toRange(location.targetSelectionRange) }
      : { uri: location.uri, range: toRange(location.range) },
  )
}

export function toEdits(edits: LspTextEdit[] | null) {
  return (edits ?? []).map((edit) => ({ text: edit.newText, range: toRange(edit.range) }))
}

/** The workspace root the client names: the directory the model's URI is in. */
export function directoryOf(uri: string): string {
  return uri.slice(0, uri.lastIndexOf('/') + 1)
}

/** The socket is on the page's own origin — inside Bambuddy's iframe that is still ScadBuddy's. */
export function socketUrl(path: string, page: string = window.location.href): string {
  const url = new URL(path, page)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

/**
 * Where the backend's bridge shows the model's pinned libraries (`LIBRARY_CLIENT_ROOT`
 * in backend/scadbuddy/library/lsp.py): `file:///libraries/<name>@<commit>/<path>`.
 * The commit is there so one URI is always one checkout's file: a model opened for
 * an earlier pin never stands in for a later one's.
 */
export const LIBRARY_ROOT = 'file:///libraries/'

/**
 * A file a definition can land in outside the open one (#185): beside the model
 * (`library` absent) or in one of its pinned libraries, at `commit`. `path` is decoded
 * and relative, `/`-separated, as the backend's file routes take it.
 */
export interface DefinitionFile {
  library?: string
  commit?: string
  path: string
}

/** A library's directory in its client URI: `<name>@<commit>`. */
const LIBRARY_DIRECTORY = /^([A-Za-z0-9][A-Za-z0-9._-]{0,63})@([0-9a-f]{40}(?:[0-9a-f]{24})?)$/

/**
 * A plain segment, as the backend's `_segments` (library/editor_files.py) takes one:
 * not empty, no `.`/`..` or dot-file, no separator or NUL once decoded. Anything else
 * could let the request URL's normalization move it off the `/files/` route.
 */
function plainSegment(segment: string): boolean {
  return segment !== '' && !segment.startsWith('.') && !/[/\\\0]/.test(segment)
}

function decodedPath(rest: string): string | null {
  try {
    const segments = rest.split('/').map(decodeURIComponent)
    return segments.every(plainSegment) ? segments.join('/') : null
  } catch {
    return null
  }
}

/** The file `uri` names under the model's `root` or a library, or null for anywhere else. */
export function definitionFile(uri: string, root: string): DefinitionFile | null {
  if (uri.startsWith(root)) {
    const path = decodedPath(uri.slice(root.length))
    return path === null ? null : { path }
  }
  if (uri.startsWith(LIBRARY_ROOT)) {
    const rest = uri.slice(LIBRARY_ROOT.length)
    const slash = rest.indexOf('/')
    if (slash <= 0) return null
    // Decoded, like the path: the bridge sends `BOSL2@<commit>`, but a Monaco URI's
    // `toString()` spells it `BOSL2%40<commit>`, and the editor opener gets that.
    const directory = LIBRARY_DIRECTORY.exec(decodedPath(rest.slice(0, slash)) ?? '')
    const path = decodedPath(rest.slice(slash + 1))
    return !directory || path === null ? null : { library: directory[1]!, commit: directory[2]!, path }
  }
  return null
}

/** How the editor names a definition's file: `helper.scad`, `BOSL2/shapes3d.scad`. */
export function definitionLabel(file: DefinitionFile): string {
  return file.library ? `${file.library}/${file.path}` : file.path
}
