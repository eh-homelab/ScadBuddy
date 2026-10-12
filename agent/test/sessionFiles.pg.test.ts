import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { AuditLog, type AuditRecord } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import type { SessionManager } from '../src/sessions/manager.js'
import type { ServerEvent } from '../src/sessions/protocol.js'
import { SessionFiles } from '../src/workspace/store.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #2329 part 3: the panel's Files, routes/sessionFiles.ts, over a real
// ai_session_files: list, download, upload and delete, with the session routes'
// guards and ownership, the caps, and the `workspace.file` event an upload logs.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const sha = (bytes: Uint8Array | string) => createHash('sha256').update(bytes).digest('hex')

type FileView = { path: string; media_type: string; size: number; sha256: string; source: Record<string, unknown> | null }

describe.skipIf(skip !== undefined)(`session files${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let files: SessionFiles
  let audit: AuditLog
  let app: ReturnType<typeof createApp>
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    audit = new AuditLog({ sql: db.sql, onError: (err) => failures.push(err) })
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    files = new SessionFiles(db.sql, { limits: { fileMaxBytes: 1024, workspaceMaxBytes: 2048 } })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, workspace: files })
    const deps: AppDeps = {
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
      workspace: files,
      audit,
    }
    app = createApp(deps)
  })
  afterEach(async () => {
    m.abortAll()
    expect(failures).toEqual([])
    await drop()
  })

  async function session(owner = browser): Promise<string> {
    const { session: s } = await m.start(owner, { origin: owner === browser ? 'chat' : 'mcp' })
    return s.id
  }

  function upload(id: string, path: string, body: Uint8Array | string, extra: Record<string, string> = {}, headers: Record<string, string> = UI) {
    const form = new FormData()
    form.set('file', new File([body], path.split('/').at(-1) ?? 'f', { type: extra.type ?? '' }))
    form.set('path', path)
    if (extra.expected_sha256 !== undefined) form.set('expected_sha256', extra.expected_sha256)
    return app.request(`/api/v1/ai/sessions/${id}/files`, { method: 'POST', headers, body: form })
  }
  const list = (id: string, headers: Record<string, string> = UI_READ) => app.request(`/api/v1/ai/sessions/${id}/files`, { headers })
  const download = (id: string, path: string, headers: Record<string, string> = UI_READ) =>
    app.request(`/api/v1/ai/sessions/${id}/files/${path}`, { headers })
  const remove = (id: string, path: string, query = '', headers: Record<string, string> = UI) =>
    app.request(`/api/v1/ai/sessions/${id}/files/${path}${query}`, { method: 'DELETE', headers })

  const logged = async (id: string): Promise<ServerEvent[]> => (await m.events.read(id, 0)).map((r) => r.event)
  const rows = async (action: string): Promise<AuditRecord[]> => (await audit.list({ kind: 'resource', action })).entries

  it('uploads, lists, downloads and deletes a file, logging and auditing each change', async () => {
    const id = await session()
    const up = await upload(id, 'uploads/part.scad', 'cube(1);')
    expect(up.status).toBe(201)
    const { file } = (await up.json()) as { file: FileView }
    expect(file).toMatchObject({ path: 'uploads/part.scad', media_type: 'text/x-openscad', size: 8, sha256: sha('cube(1);') })
    expect(file.source).toEqual({ kind: 'upload', by: 'browser', filename: 'part.scad' })

    const listed = (await (await list(id)).json()) as { files: FileView[]; usage: unknown; limits: unknown }
    expect(listed.files.map((f) => f.path)).toEqual(['uploads/part.scad'])
    expect(listed.usage).toEqual({ files: 1, bytes: 8 })
    expect(listed.limits).toEqual({ file_max_bytes: 1024, workspace_max_bytes: 2048 })

    const got = await download(id, 'uploads/part.scad')
    expect(got.status).toBe(200)
    expect(got.headers.get('content-type')).toBe('text/x-openscad')
    expect(got.headers.get('content-disposition')).toBe('attachment; filename="part.scad"')
    expect(got.headers.get('x-content-type-options')).toBe('nosniff')
    expect(got.headers.get('content-security-policy')).toBe("default-src 'none'; sandbox")
    expect(await got.text()).toBe('cube(1);')

    expect((await remove(id, 'uploads/part.scad')).status).toBe(204)
    expect(((await (await list(id)).json()) as { files: unknown[] }).files).toEqual([])
    expect((await download(id, 'uploads/part.scad')).status).toBe(404)
    expect((await remove(id, 'uploads/part.scad')).status).toBe(404)

    const events = (await logged(id)).filter((e) => e.type === 'workspace.file')
    expect(events).toEqual([
      expect.objectContaining({ path: 'uploads/part.scad', change: 'uploaded', size: 8, sha256: sha('cube(1);'), by: browser }),
      expect.objectContaining({ path: 'uploads/part.scad', change: 'deleted', by: browser }),
    ])
    await expectPanelAccepts(events)
    expect(await rows('workspace_upload')).toEqual([expect.objectContaining({ outcome: 'ok', session_id: id })])
    // The second delete found nothing: refused, and audited as such.
    expect((await rows('workspace_delete')).map((r) => [r.outcome, r.session_id]).sort()).toEqual([
      ['ok', id],
      ['refused', id],
    ])
  })

  it('never serves HTML or SVG as itself, and sniffs nothing', async () => {
    const id = await session()
    expect((await upload(id, 'page.html', '<script>alert(1)</script>', { type: 'text/html' })).status).toBe(201)
    expect((await upload(id, 'pic.svg', '<svg onload="x()"/>', { type: 'image/svg+xml' })).status).toBe(201)
    expect((await upload(id, 'img.png', PNG, { type: 'image/png' })).status).toBe(201)
    for (const [path, type] of [
      ['page.html', 'application/octet-stream'],
      ['pic.svg', 'application/octet-stream'],
      ['img.png', 'image/png'],
    ] as const) {
      const res = await download(id, path)
      expect(res.headers.get('content-type'), path).toBe(type)
      expect(res.headers.get('content-disposition'), path).toMatch(/^attachment;/)
    }
    // The stored type is kept, for the list.
    const listed = (await (await list(id)).json()) as { files: FileView[] }
    expect(listed.files.find((f) => f.path === 'page.html')?.media_type).toBe('text/html')
  })

  it('refuses to overwrite a path without its current sha256, and replaces it with one', async () => {
    const id = await session()
    expect((await upload(id, 'a.txt', 'one')).status).toBe(201)
    const again = await upload(id, 'a.txt', 'two')
    expect(again.status).toBe(409)
    expect(((await again.json()) as { detail: string }).detail).toMatch(/already exists/)
    expect((await upload(id, 'a.txt', 'two', { expected_sha256: sha('two') })).status).toBe(409)
    expect((await upload(id, 'a.txt', 'two', { expected_sha256: 'nope' })).status).toBe(400)
    const replaced = await upload(id, 'a.txt', 'two', { expected_sha256: sha('one') })
    expect(replaced.status).toBe(200)
    expect(await (await download(id, 'a.txt')).text()).toBe('two')
    expect(await rows('workspace_upload')).toEqual(
      expect.arrayContaining([expect.objectContaining({ outcome: 'refused' }), expect.objectContaining({ outcome: 'ok' })]),
    )
    // A stale sha on delete is a conflict too.
    expect((await remove(id, 'a.txt', `?expected_sha256=${sha('one')}`)).status).toBe(409)
    expect((await remove(id, 'a.txt', `?expected_sha256=${sha('two')}`)).status).toBe(204)
    expect((await remove(id, 'a.txt')).status).toBe(404)
    expect((await rows('workspace_delete')).map((r) => r.outcome).sort()).toEqual(['ok', 'refused', 'refused'])
  })

  it('applies the caps: 413 past the file cap or the workspace cap', async () => {
    const id = await session()
    expect((await upload(id, 'big.bin', new Uint8Array(1025))).status).toBe(413)
    expect((await upload(id, 'a.bin', new Uint8Array(1000))).status).toBe(201)
    expect((await upload(id, 'b.bin', new Uint8Array(1000))).status).toBe(201)
    const over = await upload(id, 'c.bin', new Uint8Array(100))
    expect(over.status).toBe(413)
    expect(((await over.json()) as { detail: string }).detail).toMatch(/at most 2048/)
    // A body far past the cap is refused before it is read.
    expect((await upload(id, 'huge.bin', new Uint8Array(200_000))).status).toBe(413)
  })

  it('refuses paths that are not workspace paths', async () => {
    const id = await session()
    for (const path of ['../x', '/etc/passwd', 'a//b', '.hidden', 'a/b/c/d/e', '']) {
      expect((await upload(id, path, 'x')).status, path).toBe(400)
    }
    expect((await download(id, '..%2F..%2Fetc%2Fpasswd')).status).toBe(400)
    expect((await download(id, '%E0%A4%A')).status).toBe(400)
    expect((await remove(id, '')).status).toBe(400)
    // Each checked path is refused and audited; an empty one is a missing field, not a path.
    expect((await rows('workspace_upload')).map((r) => r.outcome)).toEqual(Array(5).fill('refused'))
    expect((await rows('workspace_delete')).map((r) => r.outcome)).toEqual(['refused'])
  })

  it("answers a refusal with its own status when its audit row can't be written", async () => {
    const id = await session()
    expect((await upload(id, 'a.txt', 'one')).status).toBe(201)
    const record = vi.spyOn(audit, 'record').mockRejectedValue(new Error('the audit insert failed'))
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    try {
      expect((await upload(id, 'a.txt', 'two')).status).toBe(409)
      expect((await remove(id, 'missing.txt')).status).toBe(404)
      expect(quiet).toHaveBeenCalledTimes(2)
    } finally {
      record.mockRestore()
      quiet.mockRestore()
    }
  })

  it('needs a file and a path in a multipart body', async () => {
    const id = await session()
    const json = await app.request(`/api/v1/ai/sessions/${id}/files`, {
      method: 'POST',
      headers: { ...UI, 'content-type': 'application/json' },
      body: '{}',
    })
    expect(json.status).toBe(415)
    const noFile = new FormData()
    noFile.set('path', 'a.txt')
    expect((await app.request(`/api/v1/ai/sessions/${id}/files`, { method: 'POST', headers: UI, body: noFile })).status).toBe(400)
    const noPath = new FormData()
    noPath.set('file', new File(['x'], 'a.txt'))
    expect((await app.request(`/api/v1/ai/sessions/${id}/files`, { method: 'POST', headers: UI, body: noPath })).status).toBe(400)
  })

  it('keeps each session to its own files', async () => {
    const a = await session()
    const b = await session()
    expect((await upload(a, 'mine.txt', 'a')).status).toBe(201)
    expect((await download(b, 'mine.txt')).status).toBe(404)
    expect(((await (await list(b)).json()) as { files: unknown[] }).files).toEqual([])
    expect((await list('00000000-0000-4000-8000-000000000000')).status).toBe(404)
    expect((await list('not-a-session')).status).toBe(404)
  })

  it("lets the browser user read another principal's session's files but not change them until it takes it over", async () => {
    const id = await session(agentA)
    await files.write({ kind: 'session', sessionId: id }, 'theirs.txt', Buffer.from('t'))
    expect((await list(id)).status).toBe(200)
    expect((await download(id, 'theirs.txt')).status).toBe(200)
    const up = await upload(id, 'x.txt', 'x')
    expect(up.status).toBe(403)
    expect(((await up.json()) as { detail: string }).detail).toMatch(/take it over/)
    expect((await remove(id, 'theirs.txt')).status).toBe(403)
  })

  it('refuses changes to an archived session with 409', async () => {
    const id = await session()
    await m.edits.update(id, browser, { archived: true })
    expect((await upload(id, 'x.txt', 'x')).status).toBe(409)
    expect((await list(id)).status).toBe(200)
  })

  it('guards reads and writes as the session routes do', async () => {
    const id = await session()
    expect((await list(id, { ...UI_READ, 'sec-fetch-site': 'cross-site' })).status).toBe(403)
    expect((await download(id, 'a.txt', { ...UI_READ, origin: 'https://evil.example' })).status).toBe(403)
    expect((await upload(id, 'a.txt', 'x', {}, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect((await upload(id, 'a.txt', 'x', {}, { host: UI.host, 'x-forwarded-proto': 'https' })).status).toBe(403)
    // A cross-origin body past the cap is refused as cross-origin, before the cap.
    expect((await upload(id, 'huge.bin', new Uint8Array(200_000), {}, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect((await remove(id, 'a.txt', '', { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect(((await (await list(id)).json()) as { files: unknown[] }).files).toEqual([])
  })
})
