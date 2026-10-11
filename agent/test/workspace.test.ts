import { randomUUID } from 'node:crypto'
import { mkdtemp, readdir, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { existsSync, readFileSync } from 'node:fs'
import {
  describeDrained,
  downloadBudgetHook,
  downloadName,
  downloadsHook,
  downloadSource,
  type DownloadTarget,
  drainDownloads,
  MAX_DOWNLOADS_PER_CALL,
  MAX_DOWNLOADS_PER_TURN,
} from '../src/harness/browserDownloads.js'
import { NO_DOWNLOADS, TOOL_PREFIX } from '../src/harness/headlessBrowser.js'
import { runHttpRequest, withDefaults } from '../src/harness/httpRequest.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { DURABLE_TOOLS } from '../src/tools/manifest.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { safeName } from '../src/tools/workspace.js'
import { decodeText, guessMediaType, workspaceOf, workspacePath, WorkspaceError } from '../src/workspace/store.js'
import { firstText, services } from './helpers/mcp.js'

// #2329: the assistant's workspace. The parts that need no database: paths,
// media types, the tools' tiers and their refusal without a store, and
// http_request's save_to_workspace checks made before anything is sent.

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')

describe('workspace paths', () => {
  it('takes up to four safe segments and drops a leading ./', () => {
    expect(workspacePath('notes.md')).toBe('notes.md')
    expect(workspacePath('./downloads/part.3mf')).toBe('downloads/part.3mf')
    expect(workspacePath('a/b/c/d.txt')).toBe('a/b/c/d.txt')
    expect(workspacePath('_x/y-1.2')).toBe('_x/y-1.2')
  })

  it.each(['', '..', '../x', 'a/../b', '/etc/passwd', 'a//b', 'a/b/c/d/e', '.hidden', 'a/.git', 'sp ace', 'a\\b', 'x/', `${'a'.repeat(129)}`])(
    'refuses %j',
    (path) => {
      expect(() => workspacePath(path)).toThrow(WorkspaceError)
    },
  )

  it('is the session\'s inside one, and the principal\'s own outside', () => {
    const principal = { id: 'token:t1', kind: 'bearer' as const, tiers: tiersUpTo('read') }
    expect(workspaceOf({ session: 's1', principal })).toEqual({ kind: 'session', sessionId: 's1' })
    expect(workspaceOf({ principal })).toEqual({ kind: 'principal', principalKind: 'bearer', principalId: 'token:t1' })
  })
})

describe('media types', () => {
  it('sniffs images, then the extension, then text', () => {
    expect(guessMediaType('x.bin', PNG)).toBe('image/png')
    expect(guessMediaType('part.3mf', Buffer.from('PK\u0003\u0004'))).toBe('model/3mf')
    expect(guessMediaType('notes', Buffer.from('hello'))).toBe('text/plain')
    expect(guessMediaType('blob', Buffer.from([0, 1, 2]))).toBe('application/octet-stream')
  })

  it('decodes UTF-8 text but never a binary type, a NUL or invalid UTF-8', () => {
    expect(decodeText(Buffer.from('héllo'), 'text/plain')).toBe('héllo')
    expect(decodeText(Buffer.from('{"a":1}'), 'application/json')).toBe('{"a":1}')
    expect(decodeText(Buffer.from('cube(1);'), 'application/octet-stream')).toBe('cube(1);')
    expect(decodeText(Buffer.from('solid x'), 'model/stl')).toBeUndefined()
    expect(decodeText(Buffer.from([0x61, 0, 0x62]))).toBeUndefined()
    expect(decodeText(Buffer.from([0xff, 0xfe]))).toBeUndefined()
  })

  it('makes a backend file name safe for the workspace', () => {
    expect(safeName('README.md')).toBe('README.md')
    expect(safeName('../a b.png')).toBe('a-b.png')
    expect(safeName('...')).toBe('file')
  })
})

describe('the workspace tools', () => {
  const byName = (name: string) => ALL_TOOLS.find((t) => t.name === name)!

  it('are registered at their tiers, and served to durable sessions too', () => {
    const tiers = Object.fromEntries(
      ['workspace_list', 'workspace_read', 'workspace_write', 'workspace_edit', 'workspace_delete', 'workspace_save'].map((n) => [n, byName(n).risk]),
    )
    expect(tiers).toEqual({
      workspace_list: 'read',
      workspace_read: 'read',
      workspace_write: 'write',
      workspace_edit: 'write',
      workspace_delete: 'write',
      workspace_save: 'read',
    })
    for (const name of Object.keys(tiers)) expect(DURABLE_TOOLS.some((t) => t.name === name)).toBe(true)
  })

  it('refuse without a workspace store', async () => {
    const ctx: ToolContext = {
      ...services(),
      principal: { id: 'token:t1', kind: 'bearer', tiers: tiersUpTo('write') },
      progress: async () => {},
      signal: new AbortController().signal,
    }
    const result = await runTool(byName('workspace_list'), {}, ctx)
    expect(result.isError).toBe(true)
    expect(String(firstText(result))).toMatch(/needs the agent database/)
  })
})

describe('uploads take a workspace file (#2329 part 2)', () => {
  const byName = (name: string) => ALL_TOOLS.find((t) => t.name === name)!
  const UPLOADS = [
    'upload_asset',
    'add_model_media',
    'set_model_thumbnail',
    'set_output_thumbnail',
    'create_model',
    'import_model',
    'write_source_file',
    'write_file',
    'set_readme',
  ]

  it('every upload tool offers workspace_path with its sha256, says so, and keeps its tier', () => {
    for (const name of UPLOADS) {
      const t = byName(name)
      expect(Object.keys(t.shape)).toEqual(expect.arrayContaining(['workspace_path', 'workspace_sha256']))
      expect(t.description).toContain('workspace_path')
      expect(DURABLE_TOOLS.some((d) => d.name === name)).toBe(true)
    }
    expect(Object.fromEntries(UPLOADS.map((n) => [n, byName(n).risk]))).toEqual({
      upload_asset: 'write',
      add_model_media: 'write',
      set_model_thumbnail: 'write',
      set_output_thumbnail: 'write',
      create_model: 'write',
      import_model: 'outward',
      write_source_file: 'write',
      write_file: 'write',
      set_readme: 'write',
    })
  })

  it('refuses a workspace_path without a store, before anything is sent', async () => {
    const ctx: ToolContext = {
      ...services(),
      principal: { id: 'token:t1', kind: 'bearer', tiers: tiersUpTo('write') },
      progress: async () => {},
      signal: new AbortController().signal,
    }
    const result = await runTool(byName('set_readme'), { slug: 'box', workspace_path: 'r.md', workspace_sha256: 'a'.repeat(64) }, ctx)
    expect(String(firstText(result))).toMatch(/needs the agent database/)
  })
})

describe('headless-browser downloads', () => {
  it('get a plain name, as the input guard wants a filename', () => {
    expect(downloadName('part.stl')).toBe('part.stl')
    expect(downloadName('../../etc/passwd')).toBe('passwd')
    expect(downloadName('C:\\x\\My File (1).3mf')).toBe('My-File-1-.3mf')
    expect(downloadName('.hidden')).toBe('hidden')
    expect(downloadName('a..b')).toBe('a.b')
    expect(downloadName('')).toBe('download')
    expect(downloadName('...')).toBe('download')
    const long = downloadName(`${'x'.repeat(200)}.stl`)
    expect(long).toHaveLength(128)
    expect(long.endsWith('.stl')).toBe(true)
  })

  it('record their URL without its query or fragment', () => {
    expect(downloadSource('https://a.example/dl/p.stl?token=abc#frag')).toBe('https://a.example/dl/p.stl')
    expect(downloadSource('blob:https://a.example/0b6e')).toBe('blob:https://a.example')
    expect(downloadSource('data:text/plain,hi')).toBe('data:')
    expect(downloadSource('not a url')).toBe('unknown')
  })

  it('are drained after a browser call, and the model is told where they are', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'dl-'))
    const id = randomUUID()
    await writeFile(path.join(dir, `${id}.bin`), 'solid x')
    await writeFile(path.join(dir, `${id}.json`), JSON.stringify({ url: 'https://a.example/p.stl?k=1', name: 'p.stl', error: null }))
    const failed = randomUUID()
    await writeFile(path.join(dir, `${failed}.json`), JSON.stringify({ url: 'https://a.example/q', name: 'q.zip', error: 'canceled' }))
    const written: { path: string; source: unknown }[] = []
    const target: DownloadTarget = {
      ref: { kind: 'session', sessionId: 's' },
      files: {
        limits: async () => ({ fileMaxBytes: 1000, workspaceMaxBytes: 5000 }),
        usage: async () => ({ files: 0, bytes: 0 }),
        write: async (_ws, p, bytes, options) => {
          written.push({ path: p, source: options?.source })
          return { path: p, mediaType: 'model/stl', size: bytes.byteLength, sha256: 'b'.repeat(64), source: null, createdAt: new Date(), updatedAt: new Date() }
        },
      },
    }
    const hook = downloadsHook(dir, target, { waitMs: 0 })
    expect(hook.matcher).toBe(`^${TOOL_PREFIX}`)
    const signal = new AbortController().signal
    expect(await hook.hooks[0]!({ hook_event_name: 'PostToolUse', tool_name: 'mcp__scadbuddy__get_model' } as never, undefined, { signal })).toEqual({})
    const out = (await hook.hooks[0]!({ hook_event_name: 'PostToolUse', tool_name: `${TOOL_PREFIX}browser_click` } as never, undefined, {
      signal,
    })) as { hookSpecificOutput: { additionalContext: string } }
    expect(written).toEqual([{ path: 'downloads/p.stl', source: { tool: 'browser', url: 'https://a.example/p.stl' } }])
    expect(out.hookSpecificOutput.additionalContext).toContain(`downloads/p.stl (model/stl, 7 bytes, sha256 ${'b'.repeat(64)})`)
    expect(out.hookSpecificOutput.additionalContext).toContain('q.zip: not saved, the download failed in the browser')
    expect(await readdir(dir)).toEqual([])
  })

  it('are held to a budget the harness writes before every browser call: the room left in the workspace', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'budget-'))
    const file = path.join(dir, 'download-budget.json')
    let used = 4500
    const target = {
      ref: { kind: 'session', sessionId: 's' },
      files: {
        limits: async () => ({ fileMaxBytes: 1000, workspaceMaxBytes: 5000 }),
        usage: async () => ({ files: 1, bytes: used }),
        write: async () => Promise.reject(new Error('unused')),
      },
    } as unknown as DownloadTarget
    const hook = downloadBudgetHook(file, target)
    const signal = new AbortController().signal
    const before = (tool: string) => hook.hooks[0]!({ hook_event_name: 'PreToolUse', tool_name: tool } as never, undefined, { signal })
    await before('mcp__scadbuddy__get_model')
    expect(existsSync(file)).toBe(false)
    await before(`${TOOL_PREFIX}browser_click`)
    expect(JSON.parse(readFileSync(file, 'utf8'))).toEqual({
      call: 1,
      fileMaxBytes: 500,
      callMaxBytes: 500,
      callMaxCount: MAX_DOWNLOADS_PER_CALL,
      turnMaxBytes: 500,
      turnMaxCount: MAX_DOWNLOADS_PER_TURN,
    })
    used = 1000
    await before(`${TOOL_PREFIX}browser_click`)
    // The turn keeps the room it started with; a call gets what is left now.
    expect(JSON.parse(readFileSync(file, 'utf8'))).toMatchObject({ call: 2, fileMaxBytes: 1000, callMaxBytes: 4000, turnMaxBytes: 500 })
    // A workspace it cannot read allows nothing.
    target.files.usage = async () => Promise.reject(new Error('down'))
    await before(`${TOOL_PREFIX}browser_click`)
    expect(JSON.parse(readFileSync(file, 'utf8'))).toEqual({ ...NO_DOWNLOADS, call: 3 })
  })

  it('wait for a running one, and say when it is still running', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'dl-'))
    await writeFile(path.join(dir, `${randomUUID()}.pending`), '')
    const target = {
      ref: { kind: 'session', sessionId: 's' },
      files: { limits: async () => ({ fileMaxBytes: 1, workspaceMaxBytes: 1 }), write: async () => Promise.reject(new Error('no')) },
    } as unknown as DownloadTarget
    const drained = await drainDownloads(dir, target, { waitMs: 50, pollMs: 10 })
    expect(drained).toEqual({ saved: [], refused: [], running: 1 })
    expect(describeDrained(drained)).toMatch(/1 still downloading/)
    expect(describeDrained({ saved: [], refused: [], running: 0 })).toBeUndefined()
  })
})

describe('http_request save_to_workspace, before anything is sent', () => {
  const base = {
    saveDir: '/nonexistent',
    secrets: () => [],
    actor: { kind: 'browser' as const, id: 'user', label: 'User' },
  }

  it('is refused without a workspace', async () => {
    const result = await runHttpRequest(withDefaults({ url: 'http://127.0.0.1:9/', save_to_workspace: 'a.txt' }), base)
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result)).toMatch(/needs a session workspace/)
  })

  it('refuses a bad path and a HEAD', async () => {
    const workspace = { files: {} as never, ref: { kind: 'session' as const, sessionId: 's' } }
    const bad = await runHttpRequest(withDefaults({ url: 'http://127.0.0.1:9/', save_to_workspace: '../x' }), { ...base, workspace })
    expect(JSON.stringify(bad)).toMatch(/not a workspace path/)
    const head = await runHttpRequest(withDefaults({ method: 'HEAD', url: 'http://127.0.0.1:9/', save_to_workspace: 'x' }), {
      ...base,
      workspace,
    })
    expect(JSON.stringify(head)).toMatch(/HEAD request has no body/)
  })
})
