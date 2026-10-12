/**
 * #2329 — a session's workspace files, as the agent's `routes/sessionFiles.ts` serves them:
 * list, download (always an attachment), multipart upload (a taken path needs
 * `expected_sha256`, else 409) and delete, kept in memory per session. The caps are the
 * agent's defaults, or what `/api/v1/ai/settings/workspace` saved.
 */
import { HttpResponse, http } from 'msw'
import type { WorkspaceFile, WorkspaceSettings } from '../../api/types'

const MiB = 1024 * 1024
const DEFAULTS = { file_max_bytes: 25 * MiB, workspace_max_bytes: 200 * MiB }
const BOUNDS = { min: MiB, file_max: 100 * MiB, workspace_max: 2 * 1024 * MiB }
const PATH = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}(\/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}){0,3}$/

type Stored = WorkspaceFile & { bytes: Uint8Array }

const state = {
  caps: { ...DEFAULTS },
  files: new Map<string, Map<string, Stored>>(),
}

export function reset(): void {
  state.caps = { ...DEFAULTS }
  state.files.clear()
}

/** Puts a file in a session's workspace, as a tool would have (for tests). */
export function seedSessionFile(sessionId: string, path: string, bytes: Uint8Array, mediaType: string, source: WorkspaceFile['source'] = null): void {
  const now = new Date().toISOString()
  workspace(sessionId).set(path, { path, media_type: mediaType, size: bytes.byteLength, sha256: fakeSha(bytes), source, created_at: now, updated_at: now, bytes })
}

function workspace(sessionId: string): Map<string, Stored> {
  let files = state.files.get(sessionId)
  if (!files) state.files.set(sessionId, (files = new Map()))
  return files
}

/** Not a real sha256: stable for the same bytes, which is all the mock's 409 needs. */
function fakeSha(bytes: Uint8Array): string {
  let h = 2166136261
  for (const b of bytes) h = Math.imul(h ^ b, 16777619) >>> 0
  return (h.toString(16).padStart(8, '0') + bytes.byteLength.toString(16).padStart(8, '0')).repeat(4)
}

const view = ({ bytes: _bytes, ...info }: Stored): WorkspaceFile => info

/** The decoded `<path>` after `/files/`, or undefined. */
function pathOf(url: string): string | undefined {
  const pathname = new URL(url).pathname
  const at = pathname.indexOf('/files/')
  try {
    return at < 0 ? undefined : pathname.slice(at + '/files/'.length).split('/').map(decodeURIComponent).join('/')
  } catch {
    return undefined
  }
}

const settingsView = (): WorkspaceSettings => ({ ...state.caps, ...BOUNDS })

export const handlers = [
  http.get('/api/v1/ai/settings/workspace', () => HttpResponse.json(settingsView())),
  http.put('/api/v1/ai/settings/workspace', async ({ request }) => {
    const body = (await request.json()) as Partial<typeof DEFAULTS>
    const { file_max_bytes: file, workspace_max_bytes: total } = body
    const ok = (n: unknown, max: number): n is number => typeof n === 'number' && Number.isInteger(n) && n >= BOUNDS.min && n <= max
    if (!ok(file, BOUNDS.file_max) || !ok(total, BOUNDS.workspace_max) || file > total) {
      return HttpResponse.json({ detail: 'file_max_bytes and workspace_max_bytes: integers within the bounds, the file cap no larger' }, { status: 400 })
    }
    state.caps = { file_max_bytes: file, workspace_max_bytes: total }
    return HttpResponse.json(settingsView())
  }),

  http.get('/api/v1/ai/sessions/:id/files', ({ params }) => {
    const files = [...workspace(String(params.id)).values()].sort((a, b) => a.path.localeCompare(b.path))
    return HttpResponse.json({
      files: files.map(view),
      usage: { files: files.length, bytes: files.reduce((sum, f) => sum + f.size, 0) },
      limits: state.caps,
    })
  }),

  http.get('/api/v1/ai/sessions/:id/files/*', ({ params, request }) => {
    const path = pathOf(request.url)
    const file = path === undefined ? undefined : workspace(String(params.id)).get(path)
    if (!file) return HttpResponse.json({ detail: `no file ${path ?? ''} in this session's workspace` }, { status: 404 })
    const safe = /^(text\/html|image\/svg\+xml)$/.test(file.media_type) ? 'application/octet-stream' : file.media_type
    return new HttpResponse(file.bytes, {
      headers: {
        'Content-Type': safe,
        'Content-Disposition': `attachment; filename="${file.path.split('/').at(-1) ?? 'file'}"`,
        'X-Content-Type-Options': 'nosniff',
      },
    })
  }),

  http.post('/api/v1/ai/sessions/:id/files', async ({ params, request }) => {
    const form = await request.formData()
    const file = form.get('file')
    const path = form.get('path')
    const expected = form.get('expected_sha256')
    if (!(file instanceof File)) return HttpResponse.json({ detail: 'file: one file is required' }, { status: 400 })
    if (typeof path !== 'string' || !PATH.test(path)) {
      return HttpResponse.json({ detail: `${JSON.stringify(path)} is not a workspace path` }, { status: 400 })
    }
    const bytes = new Uint8Array(await file.arrayBuffer())
    if (bytes.byteLength > state.caps.file_max_bytes) {
      return HttpResponse.json({ detail: `${path} would be ${bytes.byteLength} bytes; a workspace file holds at most ${state.caps.file_max_bytes}` }, { status: 413 })
    }
    const files = workspace(String(params.id))
    const current = files.get(path)
    if (current && expected !== current.sha256) {
      return HttpResponse.json({ detail: `${path} already exists (sha256 ${current.sha256}); nothing was written` }, { status: 409 })
    }
    const held = [...files.values()].reduce((sum, f) => sum + (f.path === path ? 0 : f.size), 0)
    if (held + bytes.byteLength > state.caps.workspace_max_bytes) {
      return HttpResponse.json({ detail: `the workspace would hold ${held + bytes.byteLength} bytes; it holds at most ${state.caps.workspace_max_bytes}` }, { status: 413 })
    }
    const now = new Date().toISOString()
    const stored: Stored = {
      path,
      media_type: file.type && file.type !== 'application/octet-stream' ? file.type : 'application/octet-stream',
      size: bytes.byteLength,
      sha256: fakeSha(bytes),
      source: { kind: 'upload', by: 'browser', filename: file.name },
      created_at: current?.created_at ?? now,
      updated_at: now,
      bytes,
    }
    files.set(path, stored)
    return HttpResponse.json({ file: view(stored) }, { status: current ? 200 : 201 })
  }),

  http.delete('/api/v1/ai/sessions/:id/files/*', ({ params, request }) => {
    const path = pathOf(request.url)
    if (path === undefined || !workspace(String(params.id)).delete(path)) {
      return HttpResponse.json({ detail: `no file ${path ?? ''} in this session's workspace` }, { status: 404 })
    }
    return new HttpResponse(null, { status: 204 })
  }),
]
