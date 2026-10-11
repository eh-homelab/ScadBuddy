import { HttpResponse, http } from 'msw'
import type { SourceFile } from '../../api/types'
import { MAIN_SOURCE, MAX_SOURCE_FILES, SOURCE_FILE_PATTERN } from '../../lib/sourceFiles'
import { mockModels, mockSource, refuseBuiltin, replaceMockModel } from '../handlers'

/**
 * #185 — the files go-to-definition opens read-only (backend `api/lsp.py`):
 * `GET /models/{slug}/files/{path}` beside a model and
 * `GET /models/{slug}/libraries/{name}/files/{path}` in a library it pins. The same
 * refusals as the backend for a path that is not plain (422) or not there (404).
 *
 * #1290 — and the model's other `.scad` files (backend `api/model_files.py`): listed
 * with `GET /models/{slug}/files`, written with `PUT` and removed with `DELETE`, each
 * as one revision, a `PUT` with a stale `base` refused with 409 like the source's.
 */

const INITIAL_MODEL_FILES: Record<string, Record<string, string>> = {
  'name-keychain': {
    'helper.scad': '// Shared by the keychain\nmodule rounded_plate(size, r) {\n  offset(r) square(size);\n}\n',
  },
}

let MODEL_FILES = copyFiles()

function copyFiles(): Record<string, Record<string, string>> {
  return Object.fromEntries(Object.entries(INITIAL_MODEL_FILES).map(([slug, files]) => [slug, { ...files }]))
}

export function reset() {
  MODEL_FILES = copyFiles()
}

const LIBRARY_FILES: Record<string, Record<string, string>> = {
  BOSL2: {
    'std.scad': 'include <shapes3d.scad>\n',
    'shapes3d.scad':
      '// BOSL2 (mock)\nmodule cuboid(size, rounding = 0, anchor = CENTER) {\n  cube(size, center = true);\n}\n',
  },
}

function problem(status: number, title: string, detail: string, extra: Record<string, unknown> = {}) {
  return HttpResponse.json(
    { type: 'about:blank', title, status, detail, ...extra },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

/** The decoded path after `/files/`, or null when it is not plain. */
function plainPath(request: Request): string | null {
  const raw = new URL(request.url).pathname.split('/files/').slice(1).join('/files/')
  const segments = raw.split('/').map(decodeURIComponent)
  return segments.every((segment) => segment !== '' && !segment.startsWith('.') && !segment.includes('\\'))
    ? segments.join('/')
    : null
}

function serve(files: Record<string, string> | undefined, request: Request) {
  const path = plainPath(request)
  if (path === null) return problem(422, 'Unprocessable Content', 'not a plain path under the directory')
  const text = files?.[path]
  if (text === undefined) return problem(404, 'Not Found', `no file '${path}'`)
  return new HttpResponse(text, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
}

/** A new revision of `slug`, as a write through the history makes one. */
let revisions = 0
function commit(slug: string) {
  const model = mockModels().find((m) => m.slug === slug)
  if (!model) return undefined
  revisions += 1
  const version = `f11e${String(revisions).padStart(36, '0')}`
  return replaceMockModel({ ...model, version, updated_at: new Date().toISOString() })
}

/** The checks both writes make before the file: `require_mine`, the model, `_require_sibling`. */
function refuseWrite(slug: string, name: string) {
  const builtin = refuseBuiltin(slug)
  if (builtin) return builtin
  if (!mockModels().some((m) => m.slug === slug)) return problem(404, 'Not Found', `no model named '${slug}'`)
  if (!SOURCE_FILE_PATTERN.test(name)) return problem(422, 'Unprocessable Content', 'the file name is not a bare .scad name')
  if (name === MAIN_SOURCE) {
    return problem(409, 'Conflict', `${MAIN_SOURCE} is the model's own source: write it with PUT /models/{slug}/source`)
  }
  return undefined
}

export const handlers = [
  http.get('/api/v1/models/:slug/files', ({ params }) => {
    const slug = String(params.slug)
    const source = mockSource(slug)
    if (source === undefined) return problem(404, 'Not Found', `no model named '${slug}'`)
    const siblings = Object.entries(MODEL_FILES[slug] ?? {})
      .filter(([name]) => SOURCE_FILE_PATTERN.test(name))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, text]): SourceFile => ({ name, size: text.length, main: false }))
    return HttpResponse.json([{ name: MAIN_SOURCE, size: source.length, main: true }, ...siblings])
  }),
  http.put('/api/v1/models/:slug/files/:name', async ({ params, request }) => {
    const slug = String(params.slug)
    const name = String(params.name)
    const refused = refuseWrite(slug, name)
    if (refused) return refused
    const body = (await request.json()) as { content: string; base?: string | null }
    const current = mockModels().find((m) => m.slug === slug)?.version
    if (body.base && current && !current.startsWith(body.base)) {
      return problem(409, 'Conflict', `'${slug}' has moved on since ${body.base.slice(0, 7)}`, {
        base: body.base,
        current,
      })
    }
    const files = (MODEL_FILES[slug] ??= {})
    if (!(name in files) && Object.keys(files).length + 1 >= MAX_SOURCE_FILES) {
      return problem(422, 'Unprocessable Content', `'${slug}' already has ${MAX_SOURCE_FILES} .scad files`)
    }
    files[name] = body.content
    return HttpResponse.json(commit(slug))
  }),
  http.delete('/api/v1/models/:slug/files/:name', ({ params }) => {
    const slug = String(params.slug)
    const name = String(params.name)
    const refused = refuseWrite(slug, name)
    if (refused) return refused
    const files = MODEL_FILES[slug]
    if (!files || !(name in files)) return problem(404, 'Not Found', `'${slug}' has no file '${name}'`)
    delete files[name]
    return HttpResponse.json(commit(slug))
  }),
  http.get('/api/v1/models/:slug/libraries/:name/files/*', ({ params, request }) =>
    serve(LIBRARY_FILES[String(params.name)], request),
  ),
  http.get('/api/v1/models/:slug/files/*', ({ params, request }) =>
    serve(MODEL_FILES[String(params.slug)], request),
  ),
]
