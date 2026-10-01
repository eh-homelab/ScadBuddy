import { HttpResponse, http } from 'msw'

/**
 * #185 — the files go-to-definition opens read-only (backend `api/lsp.py`):
 * `GET /models/{slug}/files/{path}` beside a model and
 * `GET /models/{slug}/libraries/{name}/files/{path}` in a library it pins. The same
 * refusals as the backend for a path that is not plain (422) or not there (404).
 */

const MODEL_FILES: Record<string, Record<string, string>> = {
  'name-keychain': {
    'helper.scad': '// Shared by the keychain\nmodule rounded_plate(size, r) {\n  offset(r) square(size);\n}\n',
  },
}

const LIBRARY_FILES: Record<string, Record<string, string>> = {
  BOSL2: {
    'std.scad': 'include <shapes3d.scad>\n',
    'shapes3d.scad':
      '// BOSL2 (mock)\nmodule cuboid(size, rounding = 0, anchor = CENTER) {\n  cube(size, center = true);\n}\n',
  },
}

function problem(status: number, title: string, detail: string) {
  return HttpResponse.json(
    { type: 'about:blank', title, status, detail },
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

export const handlers = [
  http.get('/api/v1/models/:slug/libraries/:name/files/*', ({ params, request }) =>
    serve(LIBRARY_FILES[String(params.name)], request),
  ),
  http.get('/api/v1/models/:slug/files/*', ({ params, request }) =>
    serve(MODEL_FILES[String(params.slug)], request),
  ),
]
