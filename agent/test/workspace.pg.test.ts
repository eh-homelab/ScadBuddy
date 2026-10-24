import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { MockActivityEnvironment } from '@temporalio/testing'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import type { Database } from '../src/db.js'
import { dirBodies, HTTP_SERVER, HTTP_TOOL, runHttpRequest, withDefaults } from '../src/harness/httpRequest.js'
import { turnToolActivity } from '../src/temporal/turnTools.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { ApprovalActions, ownerOf } from '../src/approvals/mcp.js'
import { canonicalJson } from '../src/approvals/service.js'
import { DESCRIBE_CALL_ACTIVITY, gateActivities, toolActivities } from '../src/temporal/toolActivities.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { humanSize, SessionFiles, sha256, type WorkspaceRef, WorkspaceError, withWorkspaceSize } from '../src/workspace/store.js'
import { BACKEND, connect, firstText, services, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #2329: the assistant's workspace in ai_session_files: the store's caps,
// conflicts, scoping, fork copy, cascade and sweep; the workspace_* tools over
// it (in-process and over /mcp); workspace_save from the backend; and
// http_request's save_to_workspace.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const OUTPUT = 'a'.repeat(32)

const tool = (name: string) => ALL_TOOLS.find((t) => t.name === name)!

describe.skipIf(skip !== undefined)(`the session workspace${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let files: SessionFiles
  let sessionId: string
  let ws: WorkspaceRef
  let m: ReturnType<typeof manager>
  const servers: Server[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    files = new SessionFiles(db.sql, { limits: { fileMaxBytes: 1000, workspaceMaxBytes: 2500 } })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    ;({
      session: { id: sessionId },
    } = await m.start(browser, { origin: 'chat' }))
    ws = { kind: 'session', sessionId }
  })
  afterEach(async () => {
    await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))))
    await drop()
  })

  describe('the store', () => {
    it('writes, reads with its sha, lists and deletes', async () => {
      const info = await files.write(ws, 'notes/a.txt', Buffer.from('hello'), { source: { tool: 'test' } })
      expect(info).toMatchObject({ path: 'notes/a.txt', mediaType: 'text/plain', size: 5, sha256: sha256(Buffer.from('hello')) })
      const read = await files.read(ws, './notes/a.txt')
      expect(read?.bytes.toString()).toBe('hello')
      expect(read?.source).toEqual({ tool: 'test' })
      await files.write(ws, 'b.png', PNG)
      expect((await files.list(ws)).map((f) => [f.path, f.mediaType])).toEqual([
        ['b.png', 'image/png'],
        ['notes/a.txt', 'text/plain'],
      ])
      expect((await files.list(ws, 'notes')).map((f) => f.path)).toEqual(['notes/a.txt'])
      expect(await files.usage(ws)).toEqual({ files: 2, bytes: 5 + PNG.length })
      expect(await files.delete(ws, 'b.png')).toBe(true)
      expect(await files.delete(ws, 'b.png')).toBe(false)
    })

    it('holds a write to the sha it was read at, or to "not there yet"', async () => {
      const first = await files.write(ws, 'a.txt', Buffer.from('one'), { expectedSha256: null })
      await expect(files.write(ws, 'a.txt', Buffer.from('two'), { expectedSha256: null })).rejects.toMatchObject({ code: 'conflict' })
      await expect(files.write(ws, 'a.txt', Buffer.from('two'), { expectedSha256: 'f'.repeat(64) })).rejects.toMatchObject({
        code: 'conflict',
      })
      const second = await files.write(ws, 'a.txt', Buffer.from('two'), { expectedSha256: first.sha256 })
      expect(second.createdAt).toEqual(first.createdAt)
      await expect(files.delete(ws, 'a.txt', { expectedSha256: first.sha256 })).rejects.toMatchObject({ code: 'conflict' })
      expect(await files.delete(ws, 'a.txt', { expectedSha256: second.sha256 })).toBe(true)
    })

    it('caps a file and the workspace, counting a replaced file once', async () => {
      await expect(files.write(ws, 'big', Buffer.alloc(1001))).rejects.toMatchObject({ code: 'too_large' })
      await files.write(ws, 'a', Buffer.alloc(1000))
      await files.write(ws, 'b', Buffer.alloc(1000))
      await files.write(ws, 'b', Buffer.alloc(1000))
      await expect(files.write(ws, 'c', Buffer.alloc(501))).rejects.toMatchObject({ code: 'quota' })
      await files.write(ws, 'c', Buffer.alloc(500))
    })

    it('reads its caps from the settings', async () => {
      const set = new Map<string, unknown>([['workspace_file_max_bytes', 10]])
      const fromSettings = new SessionFiles(db.sql, { settings: { get: async <T>(key: string) => set.get(key) as T } })
      expect(await fromSettings.limits()).toEqual({ fileMaxBytes: 10, workspaceMaxBytes: 200 * 1024 * 1024 })
      await expect(fromSettings.write(ws, 'x', Buffer.alloc(11))).rejects.toBeInstanceOf(WorkspaceError)
    })

    it('keeps workspaces apart, copies a fork\'s, and goes with its session', async () => {
      const other: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: 'token:t1' }
      await files.write(ws, 'a.txt', Buffer.from('session'))
      await files.write(other, 'a.txt', Buffer.from('principal'))
      expect((await files.read(other, 'a.txt'))?.bytes.toString()).toBe('principal')
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
      const { session: child } = await m.start(browser, { origin: 'chat' })
      await files.copy(sessionId, child.id)
      expect((await files.read({ kind: 'session', sessionId: child.id }, 'a.txt'))?.bytes.toString()).toBe('session')
      await db.sql`DELETE FROM ai_sessions WHERE id = ${sessionId}`
      expect(await files.list(ws)).toEqual([])
      expect(await files.list(other)).toHaveLength(1)
    })

    it("sweeps a principal's files a day after their last write, never a session's", async () => {
      const other: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: 'token:t1' }
      await files.write(ws, 'old', Buffer.from('s'))
      await files.write(other, 'old', Buffer.from('p'))
      await files.write(other, 'new', Buffer.from('p'))
      await db.sql`UPDATE ai_session_files SET updated_at = now() - interval '25 hours' WHERE path = 'old'`
      expect(await files.sweep()).toBe(1)
      expect((await files.list(other)).map((f) => f.path)).toEqual(['new'])
      expect((await files.list(ws)).map((f) => f.path)).toEqual(['old'])
    })

    it('is held to safe paths by the table too', async () => {
      await expect(
        db.sql`INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, sha256, size)
               VALUES (${`session:${sessionId}`}, ${sessionId}, '../x', 'text/plain', ${Buffer.from('x')}, ${'0'.repeat(64)}, 1)`,
      ).rejects.toThrow(/check/)
    })
  })

  describe('the tools', () => {
    function ctx(fetcher?: (r: Request) => Promise<Response>, session: string | undefined = sessionId): ToolContext {
      return {
        ...services({
          workspace: files,
          ...(fetcher ? { backend: createBackendClient(BACKEND, (r) => fetcher(r as Request)) } : {}),
        }),
        principal: { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') },
        ...(session === undefined ? {} : { session }),
        progress: async () => {},
        signal: new AbortController().signal,
      }
    }

    it('write, read (paged, with the sha), edit against that sha, list and delete', async () => {
      const c = ctx()
      const written = firstText(await runTool(tool('workspace_write'), { path: 'n.md', content: 'a\nb\nc\n' }, c)) as { sha256: string }
      const page = String(firstText(await runTool(tool('workspace_read'), { path: 'n.md', offset: 2, limit: 1 }, c)))
      expect(page).toContain('     2\tb')
      expect(page).toContain(`sha256 ${written.sha256}`)
      expect(page).toContain('continue with offset 3')
      const stale = await runTool(tool('workspace_edit'), { path: 'n.md', old_string: 'b', new_string: 'B', sha256: 'f'.repeat(64) }, c)
      expect(stale.isError).toBe(true)
      const edited = firstText(
        await runTool(tool('workspace_edit'), { path: 'n.md', old_string: 'b', new_string: 'B', sha256: written.sha256 }, c),
      ) as { sha256: string; previous_sha256: string }
      expect(edited.previous_sha256).toBe(written.sha256)
      expect((await files.read(ws, 'n.md'))?.bytes.toString()).toBe('a\nB\nc\n')
      const listed = firstText(await runTool(tool('workspace_list'), {}, c)) as { files: { path: string }[]; used_bytes: number }
      expect(listed.files.map((f) => f.path)).toEqual(['n.md'])
      expect(listed.used_bytes).toBe(6)
      expect(firstText(await runTool(tool('workspace_delete'), { path: 'n.md' }, c))).toEqual({ status: 'deleted', path: 'n.md' })
      expect((await runTool(tool('workspace_delete'), { path: 'n.md' }, c)).isError).toBe(true)
    })

    it('reads an image as an image block, and other binary as metadata only', async () => {
      const c = ctx()
      await runTool(tool('workspace_write'), { path: 'p.png', content_base64: PNG.toString('base64') }, c)
      const img = await runTool(tool('workspace_read'), { path: 'p.png' }, c)
      expect(img.content.find((b) => b.type === 'image')).toMatchObject({ mimeType: 'image/png', data: PNG.toString('base64') })
      await files.write(ws, 'm.3mf', Buffer.from([0x50, 0x4b, 3, 4, 0, 0]))
      const meta = firstText(await runTool(tool('workspace_read'), { path: 'm.3mf' }, c)) as Record<string, unknown>
      expect(meta).toMatchObject({ path: 'm.3mf', media_type: 'model/3mf', size_bytes: 6 })
      expect(String(meta.content)).toMatch(/metadata only/)
    })

    it('saves an output 3MF from the backend into the workspace', async () => {
      const bytes = Buffer.from('PK\u0003\u0004 3mf bytes')
      const seen: string[] = []
      const c = ctx(async (r) => {
        seen.push(new URL(r.url).pathname)
        return new Response(bytes, { headers: { 'content-type': 'model/3mf' } })
      })
      const saved = firstText(await runTool(tool('workspace_save'), { kind: 'output_3mf', output_id: OUTPUT }, c)) as Record<string, unknown>
      expect(seen).toEqual([`/api/v1/outputs/${OUTPUT}/model.3mf`])
      expect(saved).toMatchObject({ status: 'saved', path: `${OUTPUT}.3mf`, size_bytes: bytes.length, sha256: sha256(bytes) })
      expect((await files.read(ws, `${OUTPUT}.3mf`))?.source).toEqual({ tool: 'workspace_save', kind: 'output_3mf', output_id: OUTPUT })
    })

    it('saves a model file under the path given, and refuses one over the file cap', async () => {
      const c = ctx(async (r) => {
        const path = decodeURIComponent(new URL(r.url).pathname)
        if (path === '/api/v1/models/plate/files/README.md') return new Response('# Plate\n', { headers: { 'content-type': 'text/markdown' } })
        return new Response(Buffer.alloc(2000), { headers: { 'content-type': 'video/mp4' } })
      })
      const saved = firstText(
        await runTool(tool('workspace_save'), { kind: 'model_file', slug: 'plate', file_path: 'README.md', path: 'docs/readme.md' }, c),
      ) as Record<string, unknown>
      expect(saved).toMatchObject({ path: 'docs/readme.md', media_type: 'text/markdown' })
      const missing = await runTool(tool('workspace_save'), { kind: 'model_file', slug: 'plate' }, c)
      expect(String(firstText(missing))).toMatch(/needs `file_path`/)
      const big = await runTool(tool('workspace_save'), { kind: 'print_timelapse', archive_id: 3 }, c)
      expect(big.isError).toBe(true)
      expect(String(firstText(big))).toMatch(/workspace file limit/)
    })

    it("uses the caller's own workspace outside a session, over /mcp too", async () => {
      const { app, tokens } = testApp({ services: services({ workspace: files }) })
      const { token, record } = await tokens.mint({ name: 'ws', tier: 'write' })
      const client = await connect(app, { headers: { authorization: `Bearer ${token}` } })
      try {
        const written = await client.callTool({ name: 'workspace_write', arguments: { path: 'mine.txt', content: 'hi' } })
        expect(written.isError).toBeFalsy()
        const owner: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: `token:${record.id}` }
        expect((await files.read(owner, 'mine.txt'))?.bytes.toString()).toBe('hi')
        expect(await files.list(ws)).toEqual([])
      } finally {
        await client.close()
      }
    })
  })

  describe('uploads from the workspace (#2329 part 2)', () => {
    type Seen = { method: string; path: string; type: string | null; form?: FormData; json?: unknown }
    function upload(answer: (seen: Seen) => Response = () => Response.json({ ok: true }), secrets: string[] = []) {
      const seen: Seen[] = []
      const c: ToolContext = {
        ...services({
          workspace: files,
          secrets: async () => secrets,
          backend: createBackendClient(BACKEND, async (r) => {
            const req = r as Request
            const type = req.headers.get('content-type')
            const s: Seen = { method: req.method, path: decodeURIComponent(new URL(req.url).pathname), type }
            if (type?.startsWith('multipart/form-data')) s.form = await req.formData()
            else if (type?.startsWith('application/json')) s.json = await req.json()
            seen.push(s)
            return answer(s)
          }),
        }),
        principal: { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') },
        session: sessionId,
        progress: async () => {},
        signal: new AbortController().signal,
      }
      return { c, seen }
    }

    it('streams a stored file a slice at a time, and stops at a change', async () => {
      const bytes = Buffer.from('0123456789abcdef!')
      const info = await files.write(ws, 'big.bin', bytes)
      const parts: Buffer[] = []
      for await (const part of files.chunks(ws, 'big.bin', info.sha256, 5)) parts.push(part)
      expect(parts.map((p) => p.length)).toEqual([5, 5, 5, 2])
      expect(Buffer.concat(parts).equals(bytes)).toBe(true)
      const stale = files.chunks(ws, 'big.bin', 'f'.repeat(64))
      await expect(stale.next()).rejects.toMatchObject({ code: 'conflict' })
      const changing = files.chunks(ws, 'big.bin', info.sha256, 5)
      await changing.next()
      await files.write(ws, 'big.bin', Buffer.from('other'))
      await expect(changing.next()).rejects.toMatchObject({ code: 'conflict' })
      await expect(files.chunks(ws, 'gone.bin', info.sha256).next()).rejects.toMatchObject({ code: 'not_found' })
    })

    it('upload_asset sends a workspace file as the multipart file, and names it in the result', async () => {
      const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>')
      const info = await files.write(ws, 'downloads/logo.svg', svg)
      const { c, seen } = upload(() => Response.json({ id: 'a'.repeat(64), name: 'logo.svg' }, { status: 201 }))
      const result = firstText(
        await runTool(tool('upload_asset'), { slug: 'box', workspace_path: 'downloads/logo.svg', workspace_sha256: info.sha256 }, c),
      ) as Record<string, unknown>
      expect(seen).toHaveLength(1)
      expect(seen[0]).toMatchObject({ method: 'POST', path: '/api/v1/models/box/assets' })
      expect(seen[0]!.type).toMatch(/^multipart\/form-data; boundary=/)
      const file = seen[0]!.form!.get('file') as File
      expect(file.name).toBe('logo.svg')
      expect(file.type).toBe('image/svg+xml')
      expect(Buffer.from(await file.arrayBuffer()).equals(svg)).toBe(true)
      expect(result).toMatchObject({ id: 'a'.repeat(64), workspace_file: { path: 'downloads/logo.svg', size_bytes: svg.length, sha256: info.sha256 } })
    })

    it('upload_library_file sends a workspace file with its folder, named by filename', async () => {
      const bytes = Buffer.from('3mf-bytes')
      const info = await files.write(ws, 'out/cube.3mf', bytes)
      const { c, seen } = upload(() => Response.json({ id: 11, filename: 'part.3mf', folder_id: 3 }, { status: 201 }))
      const result = firstText(
        // outward: runTool would park it for approval, which is the gate's own test
        await tool('upload_library_file').execute({ folder_id: 3, filename: 'part.3mf', workspace_path: 'out/cube.3mf', workspace_sha256: info.sha256 }, c),
      ) as Record<string, unknown>
      expect(seen[0]).toMatchObject({ method: 'POST', path: '/api/v1/print/library/files' })
      expect(seen[0]!.form!.get('folder_id')).toBe('3')
      const file = seen[0]!.form!.get('file') as File
      expect(file.name).toBe('part.3mf')
      expect(Buffer.from(await file.arrayBuffer()).equals(bytes)).toBe(true)
      expect(result).toMatchObject({ id: 11 })
    })

    it('refuses a changed file, a path without its sha256, and both or neither of the two', async () => {
      const info = await files.write(ws, 'p.png', PNG)
      const { c, seen } = upload()
      const changed = await runTool(tool('set_model_thumbnail'), { slug: 'box', workspace_path: 'p.png', workspace_sha256: 'f'.repeat(64) }, c)
      expect(String(firstText(changed))).toMatch(/changed since it was read.*nothing was sent/)
      const noSha = await runTool(tool('set_model_thumbnail'), { slug: 'box', workspace_path: 'p.png' }, c)
      expect(String(firstText(noSha))).toMatch(/needs `workspace_sha256`/)
      const both = await runTool(
        tool('set_model_thumbnail'),
        { slug: 'box', png_base64: PNG.toString('base64'), workspace_path: 'p.png', workspace_sha256: info.sha256 },
        c,
      )
      expect(String(firstText(both))).toMatch(/exactly one of the two/)
      const neither = await runTool(tool('set_model_thumbnail'), { slug: 'box' }, c)
      expect(String(firstText(neither))).toMatch(/exactly one of the two/)
      const missing = await runTool(tool('set_model_thumbnail'), { slug: 'box', workspace_path: 'nope.png', workspace_sha256: info.sha256 }, c)
      expect(String(firstText(missing))).toMatch(/no file nope.png/)
      expect(seen).toEqual([])
    })

    it('sets model and output thumbnails and adds model media from the workspace', async () => {
      const info = await files.write(ws, 'shots/p.png', PNG)
      const { c, seen } = upload((s) => (s.path.startsWith('/api/v1/outputs/') ? new Response(null, { status: 204 }) : Response.json({ slug: 'box', version: 'v2' })))
      const ref = { workspace_path: 'shots/p.png', workspace_sha256: info.sha256 }
      expect((await runTool(tool('set_model_thumbnail'), { slug: 'box', ...ref }, c)).isError).toBeFalsy()
      const out = firstText(await runTool(tool('set_output_thumbnail'), { output_id: OUTPUT, ...ref }, c)) as Record<string, unknown>
      expect(out).toMatchObject({ status: 'set', output_id: OUTPUT, workspace_file: { path: 'shots/p.png', sha256: info.sha256 } })
      expect((await runTool(tool('add_model_media'), { slug: 'box', caption: 'Front', ...ref }, c)).isError).toBeFalsy()
      expect(seen.map((s) => `${s.method} ${s.path}`)).toEqual([
        'PUT /api/v1/models/box/thumbnail',
        `PUT /api/v1/outputs/${OUTPUT}/thumbnail`,
        'POST /api/v1/models/box/media',
      ])
      for (const s of seen) {
        const file = s.form!.get('file') as File
        expect(Buffer.from(await file.arrayBuffer()).equals(PNG)).toBe(true)
      }
      expect((seen[2]!.form!.get('file') as File).name).toBe('p.png')
      expect(seen[2]!.form!.get('caption')).toBe('Front')
    })

    it('sends a text file as the text routes take it, and refuses one that is not UTF-8 text', async () => {
      const scad = Buffer.from('module part() cube(2);\n')
      const scadInfo = await files.write(ws, 'parts.scad', scad)
      const md = await files.write(ws, 'README.md', Buffer.from('# Box\n'))
      const bin = await files.write(ws, 'blob.scad', Buffer.from([0xff, 0xfe, 0x00]))
      const { c, seen } = upload((s) => Response.json({ slug: s.path.split('/')[4] ?? 'box', version: 'v2' }, { status: s.path === '/api/v1/models' ? 201 : 200 }))
      await runTool(tool('write_source_file'), { slug: 'box', name: 'parts.scad', workspace_path: 'parts.scad', workspace_sha256: scadInfo.sha256 }, c)
      await runTool(tool('set_readme'), { slug: 'box', workspace_path: 'README.md', workspace_sha256: md.sha256 }, c)
      await runTool(tool('create_model'), { name: 'Parts', workspace_path: 'parts.scad', workspace_sha256: scadInfo.sha256 }, c)
      await runTool(tool('import_model'), { workspace_path: 'parts.scad', workspace_sha256: scadInfo.sha256 }, { ...c, gate: 'harness' })
      expect(seen.map((s) => [`${s.method} ${s.path}`, s.json])).toEqual([
        ['PUT /api/v1/models/box/files/parts.scad', { content: scad.toString(), message: null }],
        ['PUT /api/v1/models/box/readme', { content: '# Box\n' }],
        ['POST /api/v1/models', { name: 'Parts', source: scad.toString(), description: '', tags: [], force: false }],
        ['POST /api/v1/models', { name: 'parts', source: scad.toString(), description: '', tags: [], force: false }],
      ])
      const refused = await runTool(tool('write_source_file'), { slug: 'box', name: 'b.scad', workspace_path: 'blob.scad', workspace_sha256: bin.sha256 }, c)
      expect(String(firstText(refused))).toMatch(/not UTF-8 text/)
      expect(seen).toHaveLength(4)
    })

    it('refuses every upload that carries a credential, from the workspace or inline, and never says which', async () => {
      const SECRET = 'sk-ant-planted-0123456789'
      const { c, seen } = upload(() => Response.json({ ok: true }), ['xy', SECRET])
      // Text, and binary with the secret across the 1 MiB slice boundary the search reads in.
      const text = await files.write(ws, 'leak.scad', Buffer.from(`// key ${SECRET}\ncube(1);\n`))
      const binary = Buffer.concat([PNG, Buffer.alloc(16, 0), Buffer.from(SECRET), Buffer.alloc(4, 0)])
      const big = new SessionFiles(db.sql, { limits: { fileMaxBytes: 3_000_000, workspaceMaxBytes: 10_000_000 } })
      const across = Buffer.concat([Buffer.alloc(1024 * 1024 - 5, 1), Buffer.from(SECRET), Buffer.alloc(10, 1)])
      const png = await files.write(ws, 'leak.png', binary)
      const spanning = await big.write(ws, 'across.bin', across)
      const fromText = { workspace_path: 'leak.scad', workspace_sha256: text.sha256 }
      const fromPng = { workspace_path: 'leak.png', workspace_sha256: png.sha256 }
      const calls: [string, Record<string, unknown>][] = [
        ['upload_asset', { slug: 'box', ...fromPng }],
        ['upload_asset', { slug: 'box', workspace_path: 'across.bin', workspace_sha256: spanning.sha256, filename: 'x.png' }],
        ['upload_asset', { slug: 'box', filename: 'x.png', content_base64: binary.toString('base64') }],
        ['set_model_thumbnail', { slug: 'box', ...fromPng }],
        ['set_model_thumbnail', { slug: 'box', png_base64: binary.toString('base64') }],
        ['set_output_thumbnail', { output_id: OUTPUT, ...fromPng }],
        ['set_output_thumbnail', { output_id: OUTPUT, png_base64: binary.toString('base64') }],
        ['add_model_media', { slug: 'box', ...fromPng }],
        ['add_model_media', { slug: 'box', filename: 'p.png', content_base64: binary.toString('base64') }],
        ['create_model', { name: 'Leak', ...fromText }],
        ['create_model', { name: 'Leak', source: `// ${SECRET}` }],
        ['import_model', fromText],
        ['write_source_file', { slug: 'box', name: 'leak.scad', ...fromText }],
        ['write_source_file', { slug: 'box', name: 'leak.scad', content: SECRET }],
        ['write_file', { slug: 'box', file_path: 'parts.scad', ...fromText }],
        ['write_file', { slug: 'box', file_path: 'parts.scad', content: `x ${SECRET} y` }],
        ['set_readme', { slug: 'box', ...fromText }],
        ['set_readme', { slug: 'box', content: SECRET }],
      ]
      for (const [name, args] of calls) {
        const result = await runTool(tool(name), args, { ...c, gate: 'harness' })
        const message = String(firstText(result))
        expect(result.isError, `${name} ${JSON.stringify(Object.keys(args))}`).toBe(true)
        expect(message).toMatch(new RegExp(`^${name}: refused, .* contains one of the agent's credentials`))
        expect(message).not.toContain(SECRET)
      }
      expect(seen).toEqual([])
      // Without the secret, the same file goes.
      await runTool(tool('set_readme'), { slug: 'box', content: 'fine' }, c)
      expect(seen).toHaveLength(1)
    })

    it('shows the path and sha256 on an approval card, never the bytes', () => {
      const sha = 'a'.repeat(64)
      const summary = tool('import_model').summarize({ workspace_path: 'downloads/box.scad', workspace_sha256: sha })
      expect(summary).toBe(`Import a model from the workspace file downloads/box.scad (sha256 ${sha})`)
      expect(tool('upload_asset').summarize({ slug: 'box', workspace_path: 'logo.svg', workspace_sha256: sha })).toContain(sha)
    })

    it('shows the size of the file the sha256 pins on the approval card, in every gate, and never hashes it (#2396)', async () => {
      // Over the suite's 1000-byte cap, so written through a store with the default caps.
      const info = await new SessionFiles(db.sql).write(ws, 'downloads/box.scad', Buffer.alloc(1234, 'a'))
      const call = { workspace_path: 'downloads/box.scad', workspace_sha256: info.sha256 }
      const size = '1.2 KB (1,234 bytes)'
      expect(humanSize(1)).toBe('1 byte')
      expect(humanSize(5 * 1024 * 1024)).toBe('5.0 MB (5,242,880 bytes)')
      expect(humanSize(3 * 1024 ** 3)).toBe('3.0 GB (3,221,225,472 bytes)')
      // Rounded before the unit is picked (#2530): never "1024.0 KB" or "1024.0 MB".
      expect(humanSize(1_048_575)).toBe('1.0 MB (1,048,575 bytes)')
      expect(humanSize(1024 ** 3 - 1)).toBe('1.0 GB (1,073,741,823 bytes)')
      expect(humanSize(1023 * 1024)).toBe('1023.0 KB (1,047,552 bytes)')
      // GB is the last unit: past it the count grows, never an index past the units (#3085).
      expect(humanSize(1024 ** 4 - 1)).toBe('1024.0 GB (1,099,511,627,775 bytes)')
      expect(humanSize(1024 ** 4)).toBe('1024.0 GB (1,099,511,627,776 bytes)')
      expect(await withWorkspaceSize(files, ws, call)).toEqual({ ...call, workspace_size: size })
      // A file that is gone or no longer at that sha256, or a bad path, is said to be; an input with no upload is left alone.
      const stale = await withWorkspaceSize(files, ws, { ...call, workspace_sha256: 'f'.repeat(64) })
      expect(stale.workspace_size).toMatch(/^no such file at this sha256/)
      expect((await withWorkspaceSize(files, ws, { ...call, workspace_path: '../x' })).workspace_size).toMatch(/^no such file/)
      expect(await withWorkspaceSize(files, ws, { slug: 'box' })).toEqual({ slug: 'box' })
      expect(await withWorkspaceSize(undefined, ws, call)).toEqual(call)
      // A size the model put in the input is never shown.
      expect(await withWorkspaceSize(files, ws, { slug: 'box', workspace_size: '1 byte' })).toEqual({ slug: 'box' })
      expect(await withWorkspaceSize(files, ws, { ...call, workspace_size: '1 byte' })).toEqual({ ...call, workspace_size: size })
      // A store that cannot be read says so; the gate still parks the call.
      const down = { stat: async () => Promise.reject(new Error('connection refused')) } as unknown as SessionFiles
      expect((await withWorkspaceSize(down, ws, call)).workspace_size).toBe('unknown: the workspace could not be read')

      // Classic: the session's approval row (the card's summary), hashed over the call's own input.
      const classic = await m.approvals.create({
        sessionId,
        turnId: null,
        toolUseId: 'toolu_up',
        tool: 'import_model',
        input: call,
        tier: 'outward',
        requestedBy: browser,
      })
      expect(JSON.parse(classic.inputSummary)).toEqual({ ...call, workspace_size: size })
      expect(classic.inputHash).toBe(m.approvals.hash('import_model', call))

      // Classic, through the turn's real gate (#2531): SessionManager hands ApprovalService
      // the session's workspace, so the parked row shows the size read there, never the
      // model's, and the hash is still over the call's own input.
      const stop = new AbortController()
      const gated = { ...call, workspace_size: '1 byte' }
      const parked = m.approvals
        .gate({ sessionId, turnId: '22222222-2222-4222-8222-222222222222', requestedBy: browser, secrets: () => [], signal: stop.signal })({
          toolName: 'import_model',
          input: gated,
          toolUseId: 'toolu_gated',
          tier: 'outward',
          signal: stop.signal,
        })
        .catch(() => undefined)
      // Named on a timeout, so a slow runner reads apart from a gate that never parked (#3085).
      const row = await vi
        .waitFor(
          async () => {
            const found = (await m.approvals.list(browser, { sessionId, pending: true })).find((a) => a.toolUseId === 'toolu_gated')
            if (!found) throw new Error('no pending approval for toolu_gated yet')
            return found
          },
          { timeout: 5000, interval: 20 },
        )
        .finally(() => stop.abort())
      await parked
      expect(JSON.parse(row.inputSummary)).toEqual({ ...call, workspace_size: size })
      expect(row.inputHash).toBe(m.approvals.hash('import_model', gated))

      // /mcp: a prepare outside any session reads the caller's own workspace.
      const principal = { id: 'token:up', kind: 'bearer' as const, tiers: tiersUpTo('outward') }
      const own: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: 'token:up' }
      const mine = await files.write(own, 'parts.scad', Buffer.from('cube(1);'))
      const mcpCall = { workspace_path: 'parts.scad', workspace_sha256: mine.sha256 }
      const parsed = { ...mcpCall, force: false } // import_model's default, as the prepare parsed it
      const prepared = await runTool(tool('import_model'), mcpCall, {
        ...services({ workspace: files, pending: new ApprovalActions(m.approvals) }),
        principal,
        progress: async () => {},
        signal: new AbortController().signal,
      })
      expect(JSON.stringify(firstText(prepared))).toContain('pending_approval')
      const [listed] = await m.approvals.listPrepared(ownerOf(principal))
      expect(JSON.parse(listed!.inputSummary)).toEqual({ ...parsed, workspace_size: '8 bytes' })
      expect(listed!.inputHash).toBe(m.approvals.hash('import_model', parsed))

      // Durable: describe_call reads the durable session's own workspace.
      const audit = {
        record: async () => {},
        hash: (_: string, input: Record<string, unknown>) => canonicalJson(input),
        summarise: (_: string, input: Record<string, unknown>) => JSON.stringify(input),
      }
      const describeCall = gateActivities({ audit, sessions: { ownerOf: async () => browser }, workspace: files })[DESCRIBE_CALL_ACTIVITY]!
      const result: unknown = await new MockActivityEnvironment({
        activityId: 'describe-1',
        workflowExecution: { workflowId: `session-${sessionId}`, runId: 'run-1' },
        activityType: DESCRIBE_CALL_ACTIVITY,
        taskQueue: 'agent-tools',
      }).run(describeCall, { tool: 'import_model', input: call })
      const described = result as { summary: string; input_hash: string }
      expect(JSON.parse(described.summary)).toEqual({ ...call, workspace_size: size })
      expect(described.input_hash).toBe(canonicalJson(call))
    })

    it('redacts the credentials from a text read', async () => {
      await files.write(ws, 'page.html', Buffer.from('<p>key sk-ant-secret-1234</p>\n'))
      const c: ToolContext = {
        ...services({ workspace: files, secrets: async () => ['sk-ant-secret-1234'] }),
        principal: { id: 'browser', kind: 'browser', tiers: tiersUpTo('read') },
        session: sessionId,
        progress: async () => {},
        signal: new AbortController().signal,
      }
      const page = String(firstText(await runTool(tool('workspace_read'), { path: 'page.html' }, c)))
      expect(page).toContain('<p>key [redacted]</p>')
      expect(page).not.toContain('sk-ant-secret-1234')
    })
  })

  describe('as durable activities on agent-tools', () => {
    it("write into the durable session's own workspace, and read it back", async () => {
      const activities = toolActivities(ALL_TOOLS, {
        services: services({ workspace: files }),
        sessions: { ownerOf: async (id) => (id === sessionId ? browser : undefined) },
      })
      const env = (activityId: string) =>
        new MockActivityEnvironment({
          activityId,
          workflowExecution: { workflowId: `session-${sessionId}`, runId: 'run-1' },
          activityType: 'workspace_write',
          taskQueue: 'agent-tools',
        })
      await env('tool-toolu_w').run(activities.workspace_write!, { path: 'd.txt', content: 'durable' })
      expect((await files.read(ws, 'd.txt'))?.bytes.toString()).toBe('durable')
      const read = await env('tool-toolu_r').run(activities.workspace_read!, { path: 'd.txt' })
      expect(unwrapUntrusted(read as string)).toContain('     1\tdurable')
    })

    it('reads an image back for view_tool_images, kept as the session\'s blob (#2402)', async () => {
      await files.write(ws, 'p.png', PNG, { mediaType: 'image/png' })
      const kept: string[] = []
      const activities = toolActivities(ALL_TOOLS, {
        services: services({ workspace: files }),
        sessions: { ownerOf: async (id) => (id === sessionId ? browser : undefined) },
        blobs: { put: async (session, images) => void kept.push(...images.map((i) => `${session}/${i.mediaType}`)) },
      })
      const read = await new MockActivityEnvironment({
        activityId: 'tool-toolu_img',
        workflowExecution: { workflowId: `session-${sessionId}`, runId: 'run-1' },
        activityType: 'workspace_read',
        taskQueue: 'agent-tools',
      }).run(activities.workspace_read!, { path: 'p.png' })
      expect(read as string).toMatch(/\[image [0-9a-f]+\.png\]/)
      expect(read as string).toContain('view_tool_images')
      expect(kept).toEqual([`${sessionId}/image/png`])
    })

    it("saves a durable http_request's body into the session's workspace (#2496)", async () => {
      const server = createServer((_req, res) => {
        res.writeHead(200, { 'content-type': 'image/png' })
        res.end(PNG)
      })
      servers.push(server)
      await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
      const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/p.png`
      const deps = {
        sessions: { ownerOf: async (id: string) => (id === sessionId ? browser : undefined) },
        servers: turnServers({
          httpRequest: { saveRoot: (await tempPaths()).stateDir, sql: db.sql, files },
          settings: { get: async <T,>() => true as T },
        }),
      }
      const out = await new MockActivityEnvironment({
        activityId: 'tool-toolu_http',
        workflowExecution: { workflowId: `session-${sessionId}`, runId: 'run-1' },
        activityType: `turn:${HTTP_SERVER}__${HTTP_TOOL}`,
        taskQueue: 'agent-tools',
      }).run(turnToolActivity(deps), { url, save_to_workspace: 'dl/p.png' })
      expect(String(out)).toContain('dl/p.png')
      expect((await files.read(ws, 'dl/p.png'))?.bytes.equals(PNG)).toBe(true)
    })

    // #2511: a binary body is a workspace file in every mode, never base64 in the result.
    async function servePng(): Promise<string> {
      const server = createServer((_req, res) => {
        res.writeHead(200, { 'content-type': 'image/png' })
        res.end(PNG)
      })
      servers.push(server)
      await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
      return `http://127.0.0.1:${(server.address() as AddressInfo).port}/p.png`
    }
    const NAME = `http/${sha256(PNG).slice(0, 12)}-p.png`

    it("stores a durable session's binary body in its workspace, and workspace_read shows it (#2511)", async () => {
      const url = await servePng()
      const deps = {
        sessions: { ownerOf: async (id: string) => (id === sessionId ? browser : undefined) },
        servers: turnServers({
          httpRequest: { saveRoot: (await tempPaths()).stateDir, sql: db.sql, files },
          settings: { get: async <T,>() => true as T },
        }),
      }
      const out = String(
        await new MockActivityEnvironment({
          activityId: 'tool-toolu_bin',
          workflowExecution: { workflowId: `session-${sessionId}`, runId: 'run-1' },
          activityType: `turn:${HTTP_SERVER}__${HTTP_TOOL}`,
          taskQueue: 'agent-tools',
        }).run(turnToolActivity(deps), { url }),
      )
      expect(out).not.toContain(PNG.toString('base64').slice(0, 16))
      expect(out).toContain(NAME)
      expect((await files.read(ws, NAME))?.bytes.equals(PNG)).toBe(true)
      const activities = toolActivities(ALL_TOOLS, {
        services: services({ workspace: files }),
        sessions: { ownerOf: async (id) => (id === sessionId ? browser : undefined) },
        blobs: { put: async () => {} },
      })
      const read = await new MockActivityEnvironment({
        activityId: 'tool-toolu_bin_read',
        workflowExecution: { workflowId: `session-${sessionId}`, runId: 'run-1' },
        activityType: 'workspace_read',
        taskQueue: 'agent-tools',
      }).run(activities.workspace_read!, { path: NAME })
      expect(read as string).toMatch(/\[image [0-9a-f]+\.png\]/)
    })

    it("stores a flow run's binary body in its starter's own workspace, which its workspace_read reads (#2511)", async () => {
      const url = await servePng()
      const flow = '00000000-0000-4000-8000-0000000000f1'
      const deps = {
        sessions: { ownerOf: async () => undefined },
        flows: { startedBy: async (run: string) => (run === flow ? browser : undefined), approved: async () => false },
        servers: turnServers({
          httpRequest: { saveRoot: (await tempPaths()).stateDir, sql: db.sql, files },
          settings: { get: async <T,>() => true as T },
        }),
      }
      const out = String(
        await new MockActivityEnvironment({
          activityId: 'tool-flow_bin',
          workflowExecution: { workflowId: `flow-${flow}`, runId: 'run-1' },
          activityType: `turn:${HTTP_SERVER}__${HTTP_TOOL}`,
          taskQueue: 'agent-tools',
        }).run(turnToolActivity(deps), { url }),
      )
      expect(out).not.toContain(PNG.toString('base64').slice(0, 16))
      expect(out).toContain(NAME)
      const own: WorkspaceRef = { kind: 'principal', principalKind: browser.kind, principalId: browser.id }
      expect((await files.read(own, NAME))?.bytes.equals(PNG)).toBe(true)
      // A flow's tool call runs in no session, as its starter: the same workspace.
      const ctx: ToolContext = {
        ...services({ workspace: files }),
        principal: { id: browser.id, kind: browser.kind, tiers: tiersUpTo('outward') },
        progress: async () => {},
        signal: new AbortController().signal,
      }
      const read = await runTool(tool('workspace_read'), { path: NAME }, ctx)
      expect(read.content.find((b) => b.type === 'image')).toMatchObject({ mimeType: 'image/png' })
      await files.delete(own, NAME)
    })
  })

  describe('http_request save_to_workspace', () => {
    async function serve(body: Buffer, type: string): Promise<string> {
      const server = createServer((_req, res) => {
        res.writeHead(200, { 'content-type': type })
        res.end(body)
      })
      servers.push(server)
      await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
      return `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    }

    const context = (dir: string) => ({
      saved: dirBodies(dir),
      secrets: () => [],
      actor: browser,
      workspace: { files, ref: ws },
    })

    it('stores the body as a workspace file, not on the disk, without the query in its source', async () => {
      const url = await serve(PNG, 'image/png')
      const dir = (await tempPaths()).stateDir
      const result = await runHttpRequest(withDefaults({ url: `${url}/img.png?token=abc`, save_to_workspace: 'dl/img.png' }), context(dir))
      const body = JSON.parse((result.content[0] as { text: string }).text) as Record<string, unknown>
      expect(body.workspace).toEqual({ name: 'dl/img.png', mediaType: 'image/png', size: PNG.length, sha256: sha256(PNG), read_with: 'workspace_read' })
      expect(body.saved).toBeNull()
      const stored = await files.read(ws, 'dl/img.png')
      expect(stored?.bytes.equals(PNG)).toBe(true)
      expect(stored?.source).toEqual({ tool: 'http_request', url: `${url}/img.png`, status: 200 })
    })

    it('reports a body over the workspace cap and keeps it on the disk as before', async () => {
      const url = await serve(Buffer.alloc(1500, 1), 'application/octet-stream')
      const dir = (await tempPaths()).stateDir
      const result = await runHttpRequest(withDefaults({ url, save_to_workspace: 'big.bin' }), context(dir))
      const body = JSON.parse((result.content[0] as { text: string }).text) as { workspace: { error: string }; saved: unknown }
      expect(body.workspace.error).toMatch(/at most 1000/)
      expect(body.saved).not.toBeNull()
    })
  })
})
