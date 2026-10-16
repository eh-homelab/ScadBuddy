import vm from 'node:vm'
import path from 'node:path'
import {
  decodeText,
  type SessionFiles,
  sniffImage,
  WorkspaceError,
  type WorkspaceFileInfo,
  type WorkspaceRef,
  workspacePath,
} from '../workspace/store.js'

// Claude Code's file tools (Read, Write, Edit, Glob, Grep, NotebookEdit) for a durable
// turn (#2393), on the session's workspace (workspace/store.ts): the durable session's
// working files, as a classic turn's are its working directory. The inputs are Claude
// Code's own (builtins/schemas.json) and the results its wording, measured against the
// bundled Claude Code 2.1.296 in a classic turn: "N\tline" for Read, "Found N file" for
// Grep, and so on. Paths are absolute under WORKSPACE_ROOT, as Claude Code's are under
// its working directory; a relative one is taken from the root.

/** Where the workspace's files appear to the model. */
export const WORKSPACE_ROOT = '/workspace'
/** Claude Code reads at most this many lines at once (its Read tool's default). */
export const READ_LINES = 2000
/** A longer line is cut, as Claude Code cuts it. */
export const READ_LINE_CHARS = 2000

export type FileToolContext = { files: SessionFiles; ws: WorkspaceRef }

/** What a built-in answers: text, an image, or a refusal the model sees as the tool's error. */
export type BuiltinResult = { text: string; isError?: boolean; image?: { data: string; mimeType: string } }

export class BuiltinError extends Error {
  override name = 'BuiltinError'
}

const ok = (text: string): BuiltinResult => ({ text })
/** As Claude Code marks an input it refuses before running the tool. */
const refused = (text: string): BuiltinResult => ({ text: `<tool_use_error>${text}</tool_use_error>`, isError: true })

/** `p` as a workspace path, or a BuiltinError saying why it is not one. */
export function toWorkspacePath(p: string): string {
  const normal = path.posix.normalize(p.trim())
  let rel: string
  if (normal === WORKSPACE_ROOT || normal === `${WORKSPACE_ROOT}/`) rel = ''
  else if (normal.startsWith(`${WORKSPACE_ROOT}/`)) rel = normal.slice(WORKSPACE_ROOT.length + 1)
  else if (normal.startsWith('/')) {
    throw new BuiltinError(`${p} is outside the session workspace: every file here is under ${WORKSPACE_ROOT}/`)
  } else rel = normal
  rel = rel.replace(/\/+$/, '')
  if (rel === '' || rel === '.') return ''
  if (rel.startsWith('..')) throw new BuiltinError(`${p} is outside the session workspace: every file here is under ${WORKSPACE_ROOT}/`)
  try {
    return workspacePath(rel)
  } catch (err) {
    if (err instanceof WorkspaceError) throw new BuiltinError(err.message)
    throw err
  }
}

const shown = (rel: string): string => (rel ? `${WORKSPACE_ROOT}/${rel}` : WORKSPACE_ROOT)

async function guarded(run: () => Promise<BuiltinResult>): Promise<BuiltinResult> {
  try {
    return await run()
  } catch (err) {
    if (err instanceof BuiltinError || err instanceof WorkspaceError) return { text: err.message, isError: true }
    throw err
  }
}

const NOT_FOUND = `File does not exist. Note: your current working directory is ${WORKSPACE_ROOT}.`

export function read(ctx: FileToolContext, input: { file_path: string; offset?: number; limit?: number }): Promise<BuiltinResult> {
  return guarded(async () => {
    const rel = toWorkspacePath(input.file_path)
    if (!rel) return { text: `EISDIR: illegal operation on a directory, read '${WORKSPACE_ROOT}'`, isError: true }
    const file = await ctx.files.read(ctx.ws, rel)
    if (!file) return { text: NOT_FOUND, isError: true }
    const image = sniffImage(file.bytes)
    if (image) return { text: `${shown(rel)} (${image})`, image: { data: file.bytes.toString('base64'), mimeType: image } }
    const text = decodeText(file.bytes, file.mediaType)
    if (text === undefined) return { text: `${shown(rel)} is a binary file (${file.mediaType}); Read reads text and images.`, isError: true }
    if (text.length === 0) return ok('<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>')
    const lines = text.split('\n')
    const start = Math.max(1, input.offset ?? 1)
    const count = Math.max(1, input.limit ?? READ_LINES)
    if (start > lines.length) {
      return ok(`<system-reminder>Warning: the file exists but is shorter than the provided offset (${start}). The file has ${lines.length} lines.</system-reminder>`)
    }
    const out: string[] = []
    for (let i = start - 1; i < Math.min(lines.length, start - 1 + count); i++) {
      const line = lines[i]!
      out.push(`${i + 1}\t${line.length > READ_LINE_CHARS ? `${line.slice(0, READ_LINE_CHARS)}... [truncated]` : line}`)
    }
    return ok(out.join('\n'))
  })
}

const updated = (rel: string) => `The file ${shown(rel)} has been updated successfully.`

export function write(ctx: FileToolContext, input: { file_path: string; content: string }): Promise<BuiltinResult> {
  return guarded(async () => {
    const rel = toWorkspacePath(input.file_path)
    if (!rel) throw new BuiltinError(`${WORKSPACE_ROOT} is a directory`)
    const before = await ctx.files.stat(ctx.ws, rel)
    await ctx.files.write(ctx.ws, rel, new TextEncoder().encode(input.content), {
      ...(before ? { mediaType: before.mediaType } : {}),
      source: before?.source ?? { tool: 'Write' },
    })
    return ok(before ? updated(rel) : `File created successfully at: ${shown(rel)}`)
  })
}

type Edit = { old_string: string; new_string: string; replace_all?: boolean | undefined }

/** `text` with each edit made in turn, or a refusal in Claude Code's words. */
export function applyEdit(text: string, edit: Edit): string | BuiltinResult {
  if (edit.old_string === edit.new_string) {
    return refused('No changes to make: old_string and new_string are exactly the same.')
  }
  if (edit.old_string === '') {
    if (text.length) return refused('Cannot create new file - file already exists.')
    return edit.new_string
  }
  const count = text.split(edit.old_string).length - 1
  if (count === 0) return refused(`String to replace not found in file.\nString: ${edit.old_string}`)
  if (count > 1 && !edit.replace_all) {
    return refused(
      `Found ${count} matches of the string to replace, but replace_all is false. To replace all occurrences, set replace_all to true. ` +
        `To replace only one occurrence, please provide more context to uniquely identify the instance.\nString: ${edit.old_string}`,
    )
  }
  return edit.replace_all ? text.split(edit.old_string).join(edit.new_string) : text.replace(edit.old_string, () => edit.new_string)
}

export function edit(ctx: FileToolContext, input: { file_path: string } & Edit): Promise<BuiltinResult> {
  return guarded(async () => {
    const rel = toWorkspacePath(input.file_path)
    if (!rel) throw new BuiltinError(`${WORKSPACE_ROOT} is a directory`)
    const file = await ctx.files.read(ctx.ws, rel)
    if (!file) {
      // As Claude Code: an empty old_string creates the file.
      if (input.old_string !== '') return { text: NOT_FOUND, isError: true }
      await ctx.files.write(ctx.ws, rel, new TextEncoder().encode(input.new_string), { source: { tool: 'Edit' }, expectedSha256: null })
      return ok(updated(rel))
    }
    const text = decodeText(file.bytes, file.mediaType)
    if (text === undefined) throw new BuiltinError(`${shown(rel)} is a binary file; Edit edits text`)
    const after = applyEdit(text, input)
    if (typeof after !== 'string') return after
    await ctx.files.write(ctx.ws, rel, new TextEncoder().encode(after), {
      mediaType: file.mediaType,
      source: file.source ?? { tool: 'Edit' },
      expectedSha256: file.sha256,
    })
    return ok(updated(rel))
  })
}

/** Workspace files under `dir` (a workspace path, '' for the root), newest first as Claude Code's Glob sorts them. */
async function filesUnder(ctx: FileToolContext, dir: string): Promise<WorkspaceFileInfo[]> {
  const all = await ctx.files.list(ctx.ws)
  const under = dir ? all.filter((f) => f.path === dir || f.path.startsWith(`${dir}/`)) : all
  return under.sort((a, b) => b.updatedAt.getTime() - a.updatedAt.getTime() || (a.path < b.path ? -1 : 1))
}

const relativeTo = (dir: string, p: string) => (dir ? (p === dir ? path.posix.basename(p) : p.slice(dir.length + 1)) : p)

/** Glob's search directory, or a refusal when it names a file. */
async function searchDir(ctx: FileToolContext, given: string | undefined): Promise<string> {
  const dir = given === undefined ? '' : toWorkspacePath(given)
  if (dir && (await ctx.files.stat(ctx.ws, dir))) throw new BuiltinError(`Path is not a directory: ${shown(dir)}`)
  return dir
}

export function glob(ctx: FileToolContext, input: { pattern: string; path?: string }): Promise<BuiltinResult> {
  return guarded(async () => {
    const dir = await searchDir(ctx, input.path)
    let pattern = input.pattern
    if (pattern.startsWith('/')) pattern = path.posix.relative(dir ? shown(dir) : WORKSPACE_ROOT, path.posix.normalize(pattern))
    const found = (await filesUnder(ctx, dir)).filter((f) => path.posix.matchesGlob(relativeTo(dir, f.path), pattern))
    if (!found.length) return ok('No files found')
    return ok(found.map((f) => (dir ? shown(f.path) : f.path)).join('\n'))
  })
}

// Grep's `type` filter, the ripgrep types a model is likely to name; any other is matched
// as an extension.
const TYPES: Record<string, string[]> = {
  js: ['js', 'mjs', 'cjs', 'jsx'],
  ts: ['ts', 'mts', 'cts', 'tsx'],
  py: ['py', 'pyi'],
  md: ['md', 'markdown'],
  json: ['json'],
  yaml: ['yaml', 'yml'],
  html: ['html', 'htm'],
  css: ['css'],
  sh: ['sh', 'bash'],
  txt: ['txt'],
  csv: ['csv'],
  scad: ['scad'],
  xml: ['xml'],
  toml: ['toml'],
  rust: ['rs'],
  go: ['go'],
  java: ['java'],
  c: ['c', 'h'],
  cpp: ['cpp', 'cc', 'cxx', 'hpp', 'hh', 'h'],
}

export type GrepInput = {
  pattern: string
  path?: string
  glob?: string
  output_mode?: 'content' | 'files_with_matches' | 'count'
  '-B'?: number
  '-A'?: number
  '-C'?: number
  context?: number
  '-n'?: boolean
  '-i'?: boolean
  '-o'?: boolean
  type?: string
  head_limit?: number
  offset?: number
  multiline?: boolean
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

/** The output's lines past `offset`, at most `head_limit` of them (0: all), as Claude Code pages Grep. */
function paged<T>(items: T[], input: GrepInput): T[] {
  const from = input.offset ?? 0
  const limit = input.head_limit ?? 250
  return items.slice(from, limit === 0 ? undefined : from + limit)
}

/** How long one Grep call may spend matching, across its files. */
export const GREP_BUDGET_MS = 2_000

// Claude Code's Grep is ripgrep, whose regex engine runs in linear time; a JavaScript
// RegExp backtracks, so a pattern such as `(a+)+$` can hold the event loop for ever.
// Matching runs under vm's timeout, which interrupts a RegExp mid-match (measured on
// Node 24: `(a+)+$` on 40 a's then b, stopped after 200 ms).
// Without multiline, ripgrep matches each line on its own, so no match crosses a newline
// (not even \s or [^x]) and the empty "line" after a final newline never matches. The
// split runs inside the vm too, so it stays under the Grep timeout; matches go
// straight into one array.
const MATCH_SCRIPT = `
  const out = []
  const at = (line, offset) => {
    for (const m of line.matchAll(re)) out.push({ index: offset + m.index, text: m[0] })
  }
  if (whole) at(text, 0)
  else {
    const lines = text.split('\\n')
    if (lines.at(-1) === '') lines.pop()
    let offset = 0
    for (const line of lines) {
      at(line, offset)
      offset += line.length + 1
    }
  }
  out`

function matchesOf(text: string, re: RegExp, whole: boolean, deadline: number): { index: number; text: string }[] {
  const timeout = Math.max(1, deadline - Date.now())
  try {
    return vm.runInNewContext(MATCH_SCRIPT, { text, re, whole }, { timeout }) as {
      index: number
      text: string
    }[]
  } catch (err) {
    if ((err as { code?: string }).code === 'ERR_SCRIPT_EXECUTION_TIMEOUT') {
      throw new BuiltinError(`Grep stopped: the pattern took longer than ${GREP_BUDGET_MS / 1000} s to match; use a simpler pattern`)
    }
    throw err
  }
}

// A newline written in a pattern: \n, \x0a, \u000a, \cJ, or octal \012 or \12 (hex
// digits and the control letter in either case; \N is just N), or the character itself.
// A class range that spans a newline without naming it ([\t-\r]) is not caught; it can
// only answer "No matches found".
const NEWLINE_ESCAPE = /^(?:n|x0[aA]|u000[aA]|c[jJ]|012|12(?!\d))/

/** Whether pattern can match a newline by naming one: anywhere but in a negated class
 * ([^\n] never matches one). Read token by token, so an escaped \\ or \[ is literal,
 * and in a class [ is only a character ([a[^\n]] is the class "a[^\n" then "]"). */
function namesNewline(pattern: string): boolean {
  let negated: boolean | undefined
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i]!
    if (c === '\\') {
      if (NEWLINE_ESCAPE.test(pattern.slice(i + 1)) && !negated) return true
      i++
    } else if (c === '\n') {
      if (!negated) return true
    } else if (negated === undefined && c === '[') {
      negated = pattern[i + 1] === '^'
      if (negated) i++
    } else if (negated !== undefined && c === ']') {
      negated = undefined
    }
  }
  return false
}

export function grep(ctx: FileToolContext, input: GrepInput): Promise<BuiltinResult> {
  return guarded(async () => {
    // Each line is matched on its own unless multiline is set (matchesOf), so ^ and $
    // already anchor per line; m would also end a line at \r, U+2028 and U+2029.
    let flags = 'g'
    if (input['-i']) flags += 'i'
    if (input.multiline) flags += 'ms'
    // Each line is matched alone, so a newline in the pattern could never match; ripgrep
    // refuses it rather than answer "No matches found".
    if (!input.multiline && namesNewline(input.pattern)) {
      throw new BuiltinError('The literal "\\n" is not allowed in a regex unless multiline is set; set multiline: true to match across lines')
    }
    let re: RegExp
    try {
      re = new RegExp(input.pattern, flags)
    } catch (err) {
      throw new BuiltinError(`Invalid regular expression: ${(err as Error).message}`)
    }
    const target = input.path === undefined ? '' : toWorkspacePath(input.path)
    const single = target ? await ctx.files.stat(ctx.ws, target) : undefined
    const candidates = single ? [single] : await filesUnder(ctx, target)
    const exts = input.type ? (TYPES[input.type] ?? [input.type]) : undefined
    const mode = input.output_mode ?? 'files_with_matches'
    const name = (p: string) => (target && !single ? shown(p) : p)
    const matched: { path: string; lines: string[]; count: number }[] = []
    const deadline = Date.now() + GREP_BUDGET_MS
    for (const info of candidates.sort((a, b) => (a.path < b.path ? -1 : 1))) {
      const base = path.posix.basename(info.path)
      if (input.glob && !path.posix.matchesGlob(base, input.glob) && !path.posix.matchesGlob(info.path, input.glob)) continue
      if (exts && !exts.includes(path.posix.extname(base).slice(1))) continue
      const file = await ctx.files.read(ctx.ws, info.path)
      const text = file && decodeText(file.bytes, file.mediaType)
      if (text === undefined || text === null || text === '') continue
      const hits = matchesOf(text, re, input.multiline === true, deadline)
      if (!hits.length) continue
      const lines: string[] = []
      if (mode === 'content') {
        const all = text.split('\n')
        const starts: number[] = [0]
        for (let i = 0; i < text.length; i++) if (text[i] === '\n') starts.push(i + 1)
        const lineOf = (index: number) => {
          let lo = 0
          let hi = starts.length - 1
          while (lo < hi) {
            const mid = (lo + hi + 1) >> 1
            if (starts[mid]! <= index) lo = mid
            else hi = mid - 1
          }
          return lo
        }
        const numbered = input['-n'] ?? true
        const ctxLines = input['-C'] ?? input.context
        const before = input['-B'] ?? ctxLines ?? 0
        const after = input['-A'] ?? ctxLines ?? 0
        const show = new Map<number, string>()
        for (const hit of hits) {
          const first = lineOf(hit.index)
          const last = lineOf(hit.index + Math.max(0, hit.text.length - 1))
          for (let l = Math.max(0, first - before); l <= Math.min(all.length - 1, last + after); l++) {
            const isMatch = l >= first && l <= last
            if (!show.has(l) || isMatch) show.set(l, isMatch ? ':' : '-')
          }
          if (input['-o']) lines.push(`${name(info.path)}:${numbered ? `${first + 1}:` : ''}${hit.text}`)
        }
        if (!input['-o']) {
          for (const l of [...show.keys()].sort((a, b) => a - b)) {
            lines.push(`${name(info.path)}${numbered ? `${show.get(l)}${l + 1}` : ''}${show.get(l)}${all[l]}`)
          }
        }
      }
      matched.push({ path: info.path, lines, count: hits.length })
    }
    if (mode === 'content') {
      const lines = paged(
        matched.flatMap((m) => m.lines),
        input,
      )
      return ok(lines.length ? lines.join('\n') : 'No matches found')
    }
    if (!matched.length) return ok(mode === 'count' ? 'No matches found' : 'No files found')
    if (mode === 'count') {
      const total = matched.reduce((n, m) => n + m.count, 0)
      const lines = paged(
        matched.map((m) => `${name(m.path)}:${m.count}`),
        input,
      )
      return ok(`${lines.join('\n')}\n\nFound ${plural(total, 'total occurrence')} across ${plural(matched.length, 'file')}.`)
    }
    const names = paged(
      matched.map((m) => name(m.path)),
      input,
    )
    return ok(`Found ${plural(matched.length, 'file')}\n${names.join('\n')}`)
  })
}

type Cell = { cell_type: string; id?: string; source: string | string[]; metadata?: unknown; outputs?: unknown[]; execution_count?: unknown }

export function notebookEdit(
  ctx: FileToolContext,
  input: { notebook_path: string; cell_id?: string; new_source: string; cell_type?: 'code' | 'markdown'; edit_mode?: 'replace' | 'insert' | 'delete' },
): Promise<BuiltinResult> {
  return guarded(async () => {
    const rel = toWorkspacePath(input.notebook_path)
    if (!rel.endsWith('.ipynb')) throw new BuiltinError('File must be a Jupyter notebook (.ipynb file). For editing other file types, use the Edit tool.')
    const file = await ctx.files.read(ctx.ws, rel)
    if (!file) return { text: 'Notebook file does not exist.', isError: true }
    let notebook: { cells: Cell[]; nbformat?: number; nbformat_minor?: number }
    try {
      notebook = JSON.parse(file.bytes.toString('utf8')) as typeof notebook
      if (!Array.isArray(notebook.cells)) throw new Error('no cells')
    } catch {
      throw new BuiltinError('Notebook is not valid JSON.')
    }
    const mode = input.edit_mode ?? 'replace'
    const at = (id: string) => {
      const byId = notebook.cells.findIndex((c) => c.id === id)
      if (byId >= 0) return byId
      const n = /^cell-(\d+)$/.exec(id)
      return n ? Number(n[1]) : -1
    }
    let index = input.cell_id === undefined ? (mode === 'insert' ? 0 : -1) : at(input.cell_id)
    if (input.cell_id !== undefined && mode === 'insert') index += 1
    let id = input.cell_id
    if (mode === 'delete') {
      if (index < 0 || index >= notebook.cells.length) throw new BuiltinError(`Cell with ID "${input.cell_id}" not found in notebook.`)
      notebook.cells.splice(index, 1)
    } else if (mode === 'insert') {
      if (!input.cell_type) throw new BuiltinError('Cell type is required when using edit_mode=insert.')
      id = Math.random().toString(36).slice(2, 10)
      const cell: Cell =
        input.cell_type === 'markdown'
          ? { cell_type: 'markdown', id, source: input.new_source, metadata: {} }
          : { cell_type: 'code', id, source: input.new_source, metadata: {}, execution_count: null, outputs: [] }
      notebook.cells.splice(Math.min(index, notebook.cells.length), 0, cell)
    } else {
      if (index < 0 || index >= notebook.cells.length) throw new BuiltinError(`Cell with ID "${input.cell_id}" not found in notebook.`)
      const cell = notebook.cells[index]!
      cell.source = input.new_source
      if (input.cell_type) cell.cell_type = input.cell_type
      if (cell.cell_type === 'code') {
        cell.execution_count = null
        cell.outputs = []
      }
    }
    await ctx.files.write(ctx.ws, rel, new TextEncoder().encode(JSON.stringify(notebook, null, 1)), {
      mediaType: file.mediaType,
      source: file.source ?? { tool: 'NotebookEdit' },
      expectedSha256: file.sha256,
    })
    if (mode === 'delete') return ok(`Deleted cell ${input.cell_id}`)
    if (mode === 'insert') return ok(`Inserted cell ${id} with ${input.new_source}`)
    return ok(`Updated cell ${input.cell_id} with ${input.new_source}`)
  })
}
