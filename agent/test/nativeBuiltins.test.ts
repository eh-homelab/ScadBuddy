import { mkdir, mkdtemp, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { NATIVE_BUILTINS, nativeBuiltinTools } from '../src/access/nativeBuiltins.js'
import { confineGuard } from '../src/harness/confine.js'

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
      ['Glob', { pattern: '*', path: outside }],
      ['Grep', { pattern: 'kek', path: outside }],
      ['Grep', { pattern: 'kek', path: path.join(root, 'dirlink') }],
      ['Read', { file_path: 42 }],
      ['Write', {}],
    ] as const) {
      expect(await guard(tool, input), `${tool} ${JSON.stringify(input)}`).toMatchObject({ deny: expect.stringContaining('session') })
    }
  })

  it('says nothing about a tool that takes no path', async () => {
    const { guard } = await setup()
    expect(await guard('mcp__scadbuddy__list_models', { path: '/etc' })).toBeUndefined()
  })
})
