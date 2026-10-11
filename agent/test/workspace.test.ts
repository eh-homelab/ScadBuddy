import { describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
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
