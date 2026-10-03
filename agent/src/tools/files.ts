import { Worker } from 'node:worker_threads'
import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import { currentOf } from './authoring.js'
import { ok } from './call.js'
import { commit, slug } from './common.js'
import { defineTool, json, text, ToolError, type Tool } from './registry.js'
import { MAX_MESSAGE_CHARS, MAX_SOURCE_CHARS } from './sourceFiles.js'

// The file tools coding agents already know (#813): Read, Edit, MultiEdit,
// Write, Glob and Grep, with their argument names, over a model's files in the
// model store. Nothing here touches a filesystem: reads go through the
// backend's file routes, and every write is one revision in the model's
// history through the same routes update_source and write_source_file use
// (model.scad parse-checked unless `force`; bundled templates refused there).
// An edit is made against the revision it read, so a write that lands between
// the read and the write is a conflict, never a silent overwrite.

const MAIN = 'model.scad'
/** The backend's SOURCE_FILE_PATTERN (api/model_files.py): what a write may name. */
const SCAD_FILE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}\.scad$/
/** As Claude Code's Read: lines per page, and characters per line before it is cut. */
const DEFAULT_LIMIT = 2000
const MAX_LINE_CHARS = 2000
/** How much of a write's diff is returned inline. */
const MAX_DIFF_CHARS = 8000
/** Grep's bounds: files fetched at once, bytes read in one search, and the time a pattern may take. */
const GREP_CONCURRENCY = 8
const MAX_GREP_BYTES = 32 * 1024 * 1024
const GREP_TIMEOUT_MS = 10_000
const DEFAULT_HEAD_LIMIT = 250

const SOURCE = "a model's files (OpenSCAD source, README, metadata) written by its author, imported from the web or pulled from an upstream"

const filePath = z
  .string()
  .min(1)
  .max(512)
  .describe('A path inside the model\'s directory, e.g. "model.scad", "parts.scad" or "README.md"')

const scadPath = filePath.describe(
  'A .scad file at the top of the model\'s directory: "model.scad" or a file beside it that it includes or uses',
)

const message = z
  .string()
  .max(MAX_MESSAGE_CHARS)
  .optional()
  .describe("What the revision is called in the history: the user's instruction, in short")

const base = commit
  .optional()
  .describe("The revision you read the file at (read_file names it). If the model has moved on since, nothing is written")

const force = z.boolean().default(false).describe('Save model.scad even when its parse check fails')

const response = z
  .enum(['diff', 'full'])
  .default('diff')
  .describe('`diff` (default) returns the revision and its diff; `full` also returns the whole file as written')

const oldString = z.string().min(1).describe('The exact text to replace')
const newString = z.string().describe('The text to replace it with')
const replaceAll = z.boolean().default(false).describe('Replace every occurrence instead of exactly one')

function normalize(path: string): string {
  return path.replace(/^(\.\/)+/, '')
}

function writable(path: string): string {
  const name = normalize(path)
  if (!SCAD_FILE.test(name)) {
    throw new ToolError(
      `${path} cannot be written by this tool: only .scad files at the top of the model's directory can ` +
        '(set_readme writes README.md, update_model its details)',
    )
  }
  return name
}

// ── pure helpers, exported for tests ─────────────────────────────────────────

/** `cat -n` style: a right-aligned line number, a tab, the line; from line `offset` (1-based), at most `limit`. */
export function numbered(content: string, offset: number, limit: number) {
  const lines = content.split('\n')
  if (lines.at(-1) === '') lines.pop()
  const from = Math.max(1, offset)
  const page = lines.slice(from - 1, from - 1 + limit)
  const text = page
    .map((line, i) => {
      const cut = line.length > MAX_LINE_CHARS ? `${line.slice(0, MAX_LINE_CHARS)}… [line cut]` : line
      return `${String(from + i).padStart(6)}\t${cut}`
    })
    .join('\n')
  return { text, from, to: from + page.length - 1, total: lines.length }
}

export type Edit = { old_string: string; new_string: string; replace_all?: boolean | undefined }

/** Exact replacements in order, all or nothing (Edit/MultiEdit). Never `String.replace`, whose `$` patterns rewrite the text. */
export function applyEdits(content: string, edits: readonly Edit[]): string {
  let out = content
  edits.forEach((edit, i) => {
    const which = edits.length > 1 ? `edit ${i + 1}: ` : ''
    if (edit.old_string === edit.new_string) throw new ToolError(`${which}old_string and new_string are the same`)
    const parts = out.split(edit.old_string)
    const matches = parts.length - 1
    if (matches === 0) throw new ToolError(`${which}old_string not found in the file; nothing was written`)
    if (matches > 1 && !edit.replace_all) {
      throw new ToolError(
        `${which}found ${matches} matches of old_string, but replace_all is false: set replace_all to replace ` +
          'them all, or give more surrounding text so it matches once. Nothing was written',
      )
    }
    out = edit.replace_all ? parts.join(edit.new_string) : `${parts[0]}${edit.new_string}${parts.slice(1).join(edit.old_string)}`
  })
  return out
}

/** A glob as a whole-name RegExp: `*`, `**`, `?`, `[…]` and `{a,b}`. */
export function globToRegExp(glob: string): RegExp {
  let out = ''
  let depth = 0
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i]!
    if (c === '*' && glob[i + 1] === '*') {
      const slash = glob[i + 2] === '/'
      out += slash ? '(?:.*/)?' : '.*'
      i += slash ? 2 : 1
    } else if (c === '*') {
      out += '[^/]*'
    } else if (c === '?') out += '[^/]'
    else if (c === '{') {
      depth++
      out += '(?:'
    } else if (c === '}' && depth > 0) {
      depth--
      out += ')'
    } else if (c === ',' && depth > 0) out += '|'
    else if (c === '[') {
      const end = glob.indexOf(']', i + 1)
      if (end === -1) out += '\\['
      else {
        out += `[${glob.slice(i + 1, end).replace(/^!/, '^').replace(/\\/g, '\\\\')}]`
        i = end
      }
    } else out += c.replace(/[.+^$()|\\]/g, '\\$&')
  }
  return new RegExp(`^${out}$`)
}

export type MatchOptions = {
  pattern: string
  ignoreCase: boolean
  mode: 'content' | 'files_with_matches' | 'count'
  before: number
  after: number
  lineNumbers: boolean
}

// Runs in a worker so a pattern that backtracks without end (`(a+)+$`) can be
// stopped: a RegExp on the main thread cannot be interrupted, and would stall
// every session this process serves.
const MATCHER = `
const { parentPort, workerData } = require('node:worker_threads')
const { files, o } = workerData
let re
try { re = new RegExp(o.pattern, o.ignoreCase ? 'i' : '') } catch (e) { parentPort.postMessage({ error: String(e.message) }); return }
const out = []
for (const f of files) {
  const lines = f.text.split('\\n').map((l) => l.replace(/\\r$/, ''))
  const hits = []
  for (let i = 0; i < lines.length; i++) if (re.test(lines[i])) hits.push(i)
  if (hits.length === 0) continue
  if (o.mode === 'files_with_matches') { out.push(f.name); continue }
  if (o.mode === 'count') { out.push(f.name + ':' + hits.length); continue }
  const shown = new Map()
  for (const h of hits) {
    for (let j = Math.max(0, h - o.before); j <= Math.min(lines.length - 1, h + o.after); j++) if (!shown.has(j)) shown.set(j, false)
    shown.set(h, true)
  }
  let last = -2
  for (const j of [...shown.keys()].sort((a, b) => a - b)) {
    if (last >= 0 && j > last + 1) out.push('--')
    const sep = shown.get(j) ? ':' : '-'
    out.push(f.name + sep + (o.lineNumbers ? (j + 1) + sep : '') + lines[j])
    last = j
  }
}
parentPort.postMessage({ out })
`

/** Grep's matching, off the main thread and bounded in time. Lines of output, in file order. */
export function matchFiles(
  files: readonly { name: string; text: string }[],
  o: MatchOptions,
  { timeoutMs = GREP_TIMEOUT_MS, signal }: { timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<string[]> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(`(() => {${MATCHER}})()`, { eval: true, workerData: { files, o } })
    const stop = (error: Error) => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', aborted)
      void worker.terminate()
      reject(error)
    }
    const aborted = () => stop(new ToolError('grep was cancelled'))
    const timer = setTimeout(
      () => stop(new ToolError(`the pattern took longer than ${timeoutMs / 1000}s to search; simplify it or narrow the search`)),
      timeoutMs,
    )
    signal?.addEventListener('abort', aborted, { once: true })
    worker.once('message', (answer: { out?: string[]; error?: string }) => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', aborted)
      void worker.terminate()
      if (answer.error !== undefined) reject(new ToolError(`invalid pattern: ${answer.error}`))
      else resolve(answer.out ?? [])
    })
    worker.once('error', (error) => stop(new ToolError(`grep failed: ${error.message}`)))
  })
}

// ── backend access ────────────────────────────────────────────────────────────

async function revisionOf(backend: BackendClient, slug: string): Promise<string | null> {
  const model = await ok(backend.GET('/api/v1/models/{slug}', { params: { path: { slug } } }), `get model ${slug}`)
  return model.version ?? null
}

async function readFile(backend: BackendClient, slug: string, path: string): Promise<string> {
  return ok(
    backend.GET('/api/v1/models/{slug}/files/{path}', { params: { path: { slug, path } }, parseAs: 'text' }),
    `read ${slug}/${path}`,
  )
}

async function scadFiles(backend: BackendClient, slug: string): Promise<string[]> {
  const files = await ok(backend.GET('/api/v1/models/{slug}/files', { params: { path: { slug } } }), `list files of ${slug}`)
  return files.map((f) => f.name)
}

function conflict(given: string, current: string | null) {
  return {
    ...json({
      status: 'conflict',
      base: given,
      current,
      next:
        'Nothing was written: the model changed after you read it. Read the file again with read_file and ' +
        'redo the change against what it holds now.',
    }),
    isError: true,
  }
}

type Write = {
  slug: string
  path: string
  content: string
  message: string | undefined
  /** The revision the write is refused unless the model is still at; null for no check. */
  base: string | null
  /** The model's revision before the write, so an identical write reads as unchanged. */
  previous: string | null
  force: boolean
  response: 'diff' | 'full'
}

/** One revision through the backend's own routes, answered with the revision and its diff. */
async function save(backend: BackendClient, w: Write) {
  if ([...w.content].length > MAX_SOURCE_CHARS) throw new ToolError(`the file would be over ${MAX_SOURCE_CHARS} characters`)
  const answered =
    w.path === MAIN
      ? await backend.PUT('/api/v1/models/{slug}/source', {
          params: { path: { slug: w.slug } },
          body: { source: w.content, message: w.message ?? null, base: w.base, force: w.force },
        })
      : await backend.PUT('/api/v1/models/{slug}/files/{name}', {
          params: { path: { slug: w.slug, name: w.path } },
          body: { content: w.content, message: w.message ?? null, base: w.base },
        })
  if (answered.response.status === 409 && w.base !== null) {
    const current = currentOf(answered.error)
    if (current !== null) return conflict(w.base, current)
  }
  const record = await ok(Promise.resolve(answered), `write ${w.slug}/${w.path}`)
  const revision = record.version ?? null
  const full = w.response === 'full' ? { content: w.content } : {}
  if (revision === null || revision === w.previous) {
    // Nothing new was committed (the file already held this), or there is no history to diff.
    return json({ status: revision === null ? 'written' : 'unchanged', slug: w.slug, file_path: w.path, revision, ...full })
  }
  const diff = await ok(
    backend.GET('/api/v1/models/{slug}/versions/{commit}/diff', { params: { path: { slug: w.slug, commit: revision } } }),
    `diff ${w.slug}@${revision}`,
  )
  const patch = diff.patch.length > MAX_DIFF_CHARS ? `${diff.patch.slice(0, MAX_DIFF_CHARS)}\n… [diff cut; diff_version has all of it]` : diff.patch
  return json({ status: 'written', slug: w.slug, file_path: w.path, revision, previous: diff.base, diff: patch, ...full })
}

/** Read the file at the model's current revision (or refuse a stale `base`), edit it, and write it back against that revision. */
async function edit(
  backend: BackendClient,
  args: { slug: string; file_path: string; edits: Edit[]; message?: string | undefined; base?: string | undefined; force: boolean; response: 'diff' | 'full' },
) {
  const path = writable(args.file_path)
  const revision = await revisionOf(backend, args.slug)
  if (args.base !== undefined && (revision === null || !revision.startsWith(args.base))) return conflict(args.base, revision)
  const before = await readFile(backend, args.slug, path)
  const after = applyEdits(before, args.edits)
  return save(backend, {
    slug: args.slug,
    path,
    content: after,
    message: args.message,
    base: revision,
    previous: revision,
    force: args.force,
    response: args.response,
  })
}

// ── grep's fan-out ────────────────────────────────────────────────────────────

async function eachLimited<T, R>(items: readonly T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array<R>(items.length)
  let next = 0
  const worker = async () => {
    while (next < items.length) {
      const i = next++
      out[i] = await fn(items[i]!)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
  return out
}

export const fileTools: Tool[] = [
  defineTool({
    name: 'read_file',
    description:
      "Read a file in a model's directory (model.scad, a .scad beside it, README.md, model.json), as " +
      '`cat -n` prints it: numbered lines, up to 2000 from `offset`. Ends with the revision it was read ' +
      'at: pass it as `base` to edit_file, multi_edit or write_file. `version` reads model.scad at an ' +
      'earlier revision (list_versions).',
    input: z.object({
      slug,
      file_path: filePath,
      offset: z.number().int().min(1).optional().describe('The line to start from (1-based)'),
      limit: z.number().int().min(1).max(10_000).optional().describe(`How many lines (${DEFAULT_LIMIT} by default)`),
      version: commit.optional().describe('An earlier revision to read model.scad at'),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/models/{slug}', 'GET /api/v1/models/{slug}/files/{path}', 'GET /api/v1/models/{slug}/versions/{commit}/source'],
    handler: async ({ slug, file_path, offset, limit, version }, { backend }) => {
      const path = normalize(file_path)
      let content: string
      let revision: string | null
      if (version !== undefined) {
        if (path !== MAIN) throw new ToolError(`only ${MAIN} can be read at an earlier revision; diff_version shows what changed in the others`)
        content = await ok(
          backend.GET('/api/v1/models/{slug}/versions/{commit}/source', { params: { path: { slug, commit: version } }, parseAs: 'text' }),
          `read ${slug}/${MAIN}@${version}`,
        )
        revision = version
      } else {
        // The revision first: a write between the two makes it older than the file, so an
        // edit based on it is refused as a conflict rather than passed.
        revision = await revisionOf(backend, slug)
        content = await readFile(backend, slug, path)
      }
      const page = numbered(content, offset ?? 1, limit ?? DEFAULT_LIMIT)
      const where =
        page.total === 0
          ? 'the file is empty'
          : page.to < page.from
            ? `offset is past the end: the file has ${page.total} lines`
            : `lines ${page.from}-${page.to} of ${page.total}${page.to < page.total ? `; continue with offset ${page.to + 1}` : ''}`
      const at = revision ? `read at revision ${revision}` : 'the model has no revision history'
      return text(`${page.text}${page.text ? '\n' : ''}[${slug}/${path}: ${where}; ${at}]`)
    },
  }),

  defineTool({
    name: 'edit_file',
    description:
      "Replace text in one of a model's .scad files, as one revision in its history: `old_string` must " +
      'match exactly once (or set `replace_all`). Returns the new revision and its diff (or the whole file ' +
      'with `response: "full"`). model.scad is parse-checked unless `force`. With `base`, refused as a ' +
      'conflict if the model has moved on since that revision.',
    input: z.object({
      slug,
      file_path: scadPath,
      old_string: oldString,
      new_string: newString,
      replace_all: replaceAll,
      message,
      base,
      force,
      response,
    }),
    risk: 'write',
    source: SOURCE,
    routes: [
      'GET /api/v1/models/{slug}',
      'GET /api/v1/models/{slug}/files/{path}',
      'PUT /api/v1/models/{slug}/source',
      'PUT /api/v1/models/{slug}/files/{name}',
      'GET /api/v1/models/{slug}/versions/{commit}/diff',
    ],
    handler: async ({ old_string, new_string, replace_all, ...args }, { backend }) =>
      edit(backend, { ...args, edits: [{ old_string, new_string, replace_all }] }),
  }),

  defineTool({
    name: 'multi_edit',
    description:
      "Several edit_file replacements in one of a model's .scad files, applied in order, all or nothing, " +
      'as ONE revision. Each edit sees the text the ones before it left.',
    input: z.object({
      slug,
      file_path: scadPath,
      edits: z
        .array(z.object({ old_string: oldString, new_string: newString, replace_all: replaceAll }))
        .min(1)
        .max(100),
      message,
      base,
      force,
      response,
    }),
    risk: 'write',
    source: SOURCE,
    routes: [
      'GET /api/v1/models/{slug}',
      'GET /api/v1/models/{slug}/files/{path}',
      'PUT /api/v1/models/{slug}/source',
      'PUT /api/v1/models/{slug}/files/{name}',
      'GET /api/v1/models/{slug}/versions/{commit}/diff',
    ],
    handler: async (args, { backend }) => edit(backend, args),
  }),

  defineTool({
    name: 'write_file',
    description:
      "Create or replace one of a model's .scad files with `content`, as one revision. Prefer edit_file " +
      'for a change to part of a file. With `base`, refused as a conflict if the model has moved on since.',
    input: z.object({
      slug,
      file_path: scadPath,
      content: z.string().describe("The file's whole new text"),
      message,
      base,
      force,
      response,
    }),
    risk: 'write',
    source: SOURCE,
    routes: [
      'GET /api/v1/models/{slug}',
      'PUT /api/v1/models/{slug}/source',
      'PUT /api/v1/models/{slug}/files/{name}',
      'GET /api/v1/models/{slug}/versions/{commit}/diff',
    ],
    handler: async ({ slug, file_path, content, message, base, force, response }, { backend }) => {
      const path = writable(file_path)
      const previous = await revisionOf(backend, slug)
      if (base !== undefined && (previous === null || !previous.startsWith(base))) return conflict(base, previous)
      return save(backend, { slug, path, content, message, base: base ?? null, previous, force, response })
    },
  }),

  defineTool({
    name: 'glob',
    description:
      "A model's .scad files whose names match `pattern` (`*.scad`, `part*`, `{a,b}.scad`): the files " +
      'model.scad can include or use. read_file also reads README.md and model.json.',
    input: z.object({ slug, pattern: z.string().min(1).max(200).describe('A glob, e.g. "*.scad"') }),
    risk: 'read',
    source: 'file names in a model directory, chosen by its author',
    routes: ['GET /api/v1/models/{slug}/files'],
    handler: async ({ slug, pattern }, { backend }) => {
      const re = globToRegExp(pattern)
      const names = (await scadFiles(backend, slug)).filter((name) => re.test(name))
      return text(names.length ? names.join('\n') : `no file in ${slug} matches ${pattern}`)
    },
  }),

  defineTool({
    name: 'grep',
    description:
      "Search model files with a regular expression (JavaScript syntax), in one model (`slug`) or across " +
      'every model in the catalogue. Searches the .scad files, filtered by `glob`, or one `path` (any text ' +
      'file). `output_mode`: `files_with_matches` (default, `slug/file` per line), `content` ' +
      '(`slug/file:line:text`, with `-A`/`-B`/`-C` context) or `count`. At most `head_limit` lines.',
    input: z.object({
      pattern: z.string().min(1).max(1000).describe('A JavaScript regular expression, matched per line'),
      slug: slug.optional().describe('Search only this model; every model when omitted'),
      path: filePath.optional().describe('Search only this file in each model'),
      glob: z.string().min(1).max(200).optional().describe('Only the .scad files whose names match, e.g. "model.scad"'),
      output_mode: z.enum(['content', 'files_with_matches', 'count']).default('files_with_matches'),
      '-i': z.boolean().default(false).describe('Case-insensitive'),
      '-n': z.boolean().default(true).describe('Line numbers in content mode'),
      '-A': z.number().int().min(0).max(50).optional().describe('Lines of context after each match'),
      '-B': z.number().int().min(0).max(50).optional().describe('Lines of context before each match'),
      '-C': z.number().int().min(0).max(50).optional().describe('Lines of context around each match'),
      head_limit: z.number().int().min(1).max(5000).default(DEFAULT_HEAD_LIMIT).describe('At most this many lines of output'),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/models', 'GET /api/v1/models/{slug}/files', 'GET /api/v1/models/{slug}/files/{path}'],
    handler: async (args, { backend, signal }) => {
      const slugs = args.slug !== undefined ? [args.slug] : (await ok(backend.GET('/api/v1/models'), 'list models')).map((m) => m.slug)
      const only = args.glob !== undefined ? globToRegExp(args.glob) : null
      const targets = (
        await eachLimited(slugs, GREP_CONCURRENCY, async (s) => {
          if (args.path !== undefined) return [{ slug: s, name: normalize(args.path) }]
          const names = await scadFiles(backend, s)
          return names.filter((n) => only === null || only.test(n)).map((name) => ({ slug: s, name }))
        })
      ).flat()
      let bytes = 0
      let cut = false
      const files = (
        await eachLimited(targets, GREP_CONCURRENCY, async ({ slug: s, name }) => {
          if (cut) return null
          try {
            const content = await readFile(backend, s, name)
            bytes += content.length
            if (bytes > MAX_GREP_BYTES) {
              cut = true
              return null
            }
            return { name: `${s}/${name}`, text: content }
          } catch (error) {
            // One file of one model gone or not text is not the search's failure; a named
            // file in a single model is.
            if (args.slug !== undefined && args.path !== undefined) throw error
            return null
          }
        })
      ).filter((f) => f !== null)
      const context = args['-C'] ?? 0
      const lines = await matchFiles(
        files,
        {
          pattern: args.pattern,
          ignoreCase: args['-i'],
          mode: args.output_mode,
          before: args['-B'] ?? context,
          after: args['-A'] ?? context,
          lineNumbers: args['-n'],
        },
        { signal },
      )
      const shown = lines.slice(0, args.head_limit)
      const notes = [
        ...(lines.length > shown.length ? [`${lines.length - shown.length} more lines past head_limit ${args.head_limit}`] : []),
        ...(cut ? [`stopped reading after ${MAX_GREP_BYTES / 1024 / 1024} MiB; narrow it with slug, glob or path`] : []),
      ]
      const body = shown.length ? shown.join('\n') : `no matches in ${files.length} file(s)`
      return text(notes.length ? `${body}\n[${notes.join('; ')}]` : body)
    },
  }),
]
