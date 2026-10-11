import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { MockActivityEnvironment } from '@temporalio/testing'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import type { Database } from '../src/db.js'
import { runHttpRequest, withDefaults } from '../src/harness/httpRequest.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { SessionFiles, sha256, type WorkspaceRef, WorkspaceError } from '../src/workspace/store.js'
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
  const servers: Server[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    files = new SessionFiles(db.sql, { limits: { fileMaxBytes: 1000, workspaceMaxBytes: 2500 } })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
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
      saveDir: dir,
      secrets: () => [],
      actor: browser,
      workspace: { files, ref: ws },
    })

    it('stores the body as a workspace file, not on the disk, without the query in its source', async () => {
      const url = await serve(PNG, 'image/png')
      const dir = (await tempPaths()).stateDir
      const result = await runHttpRequest(withDefaults({ url: `${url}/img.png?token=abc`, save_to_workspace: 'dl/img.png' }), context(dir))
      const body = JSON.parse((result.content[0] as { text: string }).text) as Record<string, unknown>
      expect(body.workspace).toMatchObject({ path: 'dl/img.png', size_bytes: PNG.length, media_type: 'image/png', sha256: sha256(PNG) })
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
