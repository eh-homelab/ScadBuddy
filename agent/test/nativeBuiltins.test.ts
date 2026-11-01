import { chmod, lstat, mkdir, mkdtemp, readFile, readlink, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import type { AccessRow } from '../src/access/model.js'
import { NATIVE_BUILTINS, nativeBuiltinTools } from '../src/access/nativeBuiltins.js'
import { AccessResolver } from '../src/access/resolver.js'
import { confineGuard } from '../src/harness/confine.js'
import { claudeConfigDir } from '../src/harness/options.js'
import { TASK_LIST_DIR, ensureStateDirs, linkTaskList } from '../src/harness/stateDirs.js'

// Plan 2026-10-13 Step 9.6 (decisions Q3, Q4): Claude Code's own built-ins in every session,
// their access under `builtin:claude-code`, the file tools confined to the session directory.

describe("Claude Code's built-ins by default", () => {
  it('offers the file tools and the task list, at read or write, and never Bash or PowerShell', async () => {
    const offered = await nativeBuiltinTools({ native: async (_name, declared) => declared }, [])
    expect(Object.fromEntries(offered.map((t) => [t.name, t.tier]))).toEqual({
      Read: 'read',
      Glob: 'read',
      Grep: 'read',
      TaskCreate: 'read',
      TaskGet: 'read',
      TaskList: 'read',
      TaskUpdate: 'read',
      Write: 'write',
      Edit: 'write',
      NotebookEdit: 'write',
    })
    expect(NATIVE_BUILTINS.find((b) => b.name === 'Bash')?.access).toBe('off')
    expect(NATIVE_BUILTINS.find((b) => b.name === 'PowerShell')?.access).toBe('off')
  })

  it('leaves out one set off, and a package that names an off one switches it on at its own tier', async () => {
    const offered = await nativeBuiltinTools(
      {
        native: async (name, declared, packages) => (name === 'Grep' ? 'off' : declared === 'off' && packages.length ? 'outward' : declared),
      },
      [{ name: 'Bash', packages: ['shell-pkg'] }],
    )
    const names = offered.map((t) => t.name)
    expect(names).not.toContain('Grep')
    expect(names).not.toContain('PowerShell')
    expect(offered.find((t) => t.name === 'Bash')).toEqual({ name: 'Bash', tier: 'outward' })
  })
})

describe('the file tools are confined to the session directory', () => {
  const setup = async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'confine-'))
    const outside = await mkdtemp(path.join(os.tmpdir(), 'confine-out-'))
    await writeFile(path.join(outside, 'secret'), 'kek')
    await mkdir(path.join(root, 'notes'))
    await writeFile(path.join(root, 'notes', 'a.txt'), 'a')
    await symlink(path.join(outside, 'secret'), path.join(root, 'link'))
    await symlink(outside, path.join(root, 'dirlink'))
    return { root, outside, guard: confineGuard(root) }
  }

  it('lets a path inside it through, relative or absolute, and a new file in it', async () => {
    const { root, guard } = await setup()
    for (const [tool, input] of [
      ['Read', { file_path: path.join(root, 'notes', 'a.txt') }],
      ['Read', { file_path: 'notes/a.txt' }],
      ['Write', { file_path: path.join(root, 'new', 'deep', 'b.txt'), content: 'b' }],
      ['Edit', { file_path: path.join(root, 'notes', 'a.txt'), old_string: 'a', new_string: 'b' }],
      ['NotebookEdit', { notebook_path: path.join(root, 'n.ipynb'), new_source: 'x' }],
      ['Glob', { pattern: '**/*.txt' }],
      ['Glob', { pattern: '*.txt', path: path.join(root, 'notes') }],
      ['Grep', { pattern: 'a' }],
      ['Grep', { pattern: 'a', path: 'notes' }],
      ['TaskList', {}],
    ] as const) {
      expect(await guard(tool, input), `${tool} ${JSON.stringify(input)}`).toBeUndefined()
    }
  })

  it('refuses a path outside it, by `..`, by an absolute path, or through a link', async () => {
    const { root, outside, guard } = await setup()
    for (const [tool, input] of [
      ['Read', { file_path: '/etc/hostname' }],
      ['Read', { file_path: '/etc/scadbuddy/kek/kek' }],
      ['Read', { file_path: '../secret' }],
      ['Read', { file_path: path.join(root, 'link') }],
      ['Read', { file_path: path.join(root, 'dirlink', 'secret') }],
      ['Write', { file_path: path.join(outside, 'new.txt'), content: 'x' }],
      ['Write', { file_path: path.join(root, 'dirlink', 'new.txt'), content: 'x' }],
      ['Edit', { file_path: path.join(root, 'link'), old_string: 'k', new_string: 'x' }],
      ['NotebookEdit', { notebook_path: '/tmp/n.ipynb', new_source: 'x' }],
      ['Glob', { pattern: '/etc/*' }],
      ['Glob', { pattern: '../*' }],
      ['Glob', { pattern: path.join(root, 'dirlink', '*') }],
      ['Glob', { pattern: '*', path: outside }],
      ['Grep', { pattern: 'kek', path: outside }],
      ['Grep', { pattern: 'kek', path: path.join(root, 'dirlink') }],
      ['Read', { file_path: 42 }],
      ['Write', {}],
    ] as const) {
      expect(await guard(tool, input), `${tool} ${JSON.stringify(input)}`).toMatchObject({ deny: expect.stringContaining('session') })
    }
  })

  // Claude Code expands a leading `~` to HOME (the state dir: its config, transcripts and
  // credentials), and Glob and Grep do it after the seam has seen the unexpanded path
  // (measured on 2.1.310, test/nativeBuiltins.sdk.test.ts). Every `~` first segment is refused.
  it('refuses a path that starts at a home directory, in every path input', async () => {
    const { guard } = await setup()
    for (const [tool, input] of [
      ['Read', { file_path: '~/x' }],
      ['Read', { file_path: '~' }],
      ['Read', { file_path: '~root/x' }],
      ['Write', { file_path: '~/new.txt', content: 'x' }],
      ['Edit', { file_path: '~/.bashrc', old_string: 'a', new_string: 'b' }],
      ['NotebookEdit', { notebook_path: '~/n.ipynb', new_source: 'x' }],
      ['Glob', { pattern: '*', path: '~' }],
      ['Glob', { pattern: '*', path: '~/.' }],
      ['Glob', { pattern: '~/*' }],
      ['Grep', { pattern: 'k', path: '~' }],
      ['Grep', { pattern: 'k', path: '~root' }],
      ['Grep', { pattern: 'k', glob: '~/x' }],
    ] as const) {
      expect(await guard(tool, input), `${tool} ${JSON.stringify(input)}`).toMatchObject({ deny: expect.stringContaining('session') })
    }
    // A `~` inside a name, not at its start, is a file in the directory.
    expect(await guard('Read', { file_path: 'notes/~a.txt' })).toBeUndefined()
  })

  it('refuses every file tool when there is no session directory', async () => {
    const guard = confineGuard(undefined)
    expect(await guard('Read', { file_path: '/tmp/x' })).toMatchObject({ deny: expect.stringContaining('no working directory') })
    expect(await guard('Glob', { pattern: '*' })).toMatchObject({ deny: expect.stringContaining('no working directory') })
    expect(await guard('TaskList', {})).toBeUndefined()
  })

  it('says nothing about a tool that takes no path', async () => {
    const { guard } = await setup()
    expect(await guard('mcp__scadbuddy__list_models', { path: '/etc' })).toBeUndefined()
  })
})

describe("the task list's link into the session directory", () => {
  const SESSION = '0f0e0d0c-0b0a-4908-8706-050403020100'
  const setup = async () => {
    const paths = { stateDir: await mkdtemp(path.join(os.tmpdir(), 'tasks-state-')) }
    await ensureStateDirs(paths)
    const sessionDir = await mkdtemp(path.join(os.tmpdir(), 'tasks-cwd-'))
    const old = path.join(claudeConfigDir(paths), 'tasks', SESSION)
    await mkdir(old, { recursive: true })
    await writeFile(path.join(old, '1.json'), '{"subject":"kept"}')
    return { paths, sessionDir, old }
  }

  it("moves an older image's list in, then links it", async () => {
    const { paths, sessionDir, old } = await setup()
    await linkTaskList(paths, sessionDir, SESSION)
    expect(await readlink(old)).toBe(path.join(sessionDir, TASK_LIST_DIR))
    expect(await readFile(path.join(sessionDir, TASK_LIST_DIR, '1.json'), 'utf8')).toContain('kept')
  })

  it('keeps the old list where it is when a file cannot be moved', async () => {
    const { paths, sessionDir, old } = await setup()
    await mkdir(path.join(sessionDir, TASK_LIST_DIR), { recursive: true })
    await chmod(path.join(sessionDir, TASK_LIST_DIR), 0o500)
    try {
      await linkTaskList(paths, sessionDir, SESSION)
    } finally {
      await chmod(path.join(sessionDir, TASK_LIST_DIR), 0o700)
    }
    expect((await lstat(old)).isDirectory()).toBe(true)
    expect(await readFile(path.join(old, '1.json'), 'utf8')).toContain('kept')
  })
})

describe('AccessResolver.native, over stored rows', () => {
  const CC = 'builtin:claude-code'
  const row = (source: string, item: string, access: string): AccessRow => ({ source, item, access, updated_by: 'test' })
  const resolver = (rows: AccessRow[]) => new AccessResolver({ rows: (sources) => Promise.resolve(rows.filter((r) => sources.includes(r.source))) })

  it('is the default with no row, and the row over the default', async () => {
    expect(await resolver([]).native('Read', 'read', [])).toBe('read')
    expect(await resolver([row(CC, 'Write', 'outward')]).native('Write', 'write', [])).toBe('outward')
    expect(await resolver([row(CC, 'Read', 'off')]).native('Read', 'read', [])).toBe('off')
  })

  it("switches a built-in off by default on for a package that names it, at the package's tier", async () => {
    expect(await resolver([]).native('Bash', 'off', ['shell'])).toBe('outward')
    expect(await resolver([row('package:shell', 'builtin:Bash', 'write')]).native('Bash', 'off', ['shell'])).toBe('write')
  })

  it('keeps it off for a package when the source is switched off, or its own row says off', async () => {
    expect(await resolver([row(CC, '*', 'off')]).native('Bash', 'off', ['shell'])).toBe('off')
    expect(await resolver([row(CC, '*', 'off')]).native('Read', 'read', ['shell'])).toBe('off')
    expect(await resolver([row(CC, 'Bash', 'off')]).native('Bash', 'off', ['shell'])).toBe('off')
  })
})
