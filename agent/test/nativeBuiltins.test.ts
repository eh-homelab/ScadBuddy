import { chmod, lstat, mkdir, mkdtemp, readFile, readlink, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import type { AccessRow } from '../src/access/model.js'
import { monitorAccess, NATIVE_BUILTINS, SESSION_BUILTINS, sessionTierOf, turnBuiltins } from '../src/access/nativeBuiltins.js'
import { BACKGROUND_BUILTINS, BUILTINS } from '../src/builtins/server.js'
import { AccessResolver } from '../src/access/resolver.js'
import { confineGuard, projectDirName } from '../src/harness/confine.js'
import { claudeConfigDir } from '../src/harness/options.js'
import { TASK_LIST_DIR, ensureStateDirs, linkTaskList } from '../src/harness/stateDirs.js'

// Plan 2026-10-13 Step 9.6 (decisions Q3, Q4): Claude Code's own built-ins in every session,
// their access under `builtin:claude-code`, the file tools confined to the session directory.

describe("Claude Code's built-ins by default", () => {
  it('offers the file tools and the task list, at read or write, and never Bash or PowerShell', async () => {
    const { native: offered } = await turnBuiltins({ native: async (_name, declared) => declared }, [], { background: true })
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
    const { native: offered } = await turnBuiltins(
      {
        native: async (name, declared, packages) => (name === 'Grep' ? 'off' : declared === 'off' && packages.length ? 'outward' : declared),
      },
      [{ name: 'Bash', packages: ['shell-pkg'] }],
      { background: true },
    )
    const names = offered.map((t) => t.name)
    expect(names).not.toContain('Grep')
    expect(names).not.toContain('PowerShell')
    expect(offered.find((t) => t.name === 'Bash')).toEqual({ name: 'Bash', tier: 'outward' })
  })
})

describe("the session's own built-ins by default (#2726 R2)", () => {
  const declared = { native: async (_name: string, access: 'off' | 'read' | 'write' | 'outward') => access }

  it('are the built-ins the session serves, the background ones marked as server.ts marks them', () => {
    expect(SESSION_BUILTINS.map((b) => b.name).sort()).toEqual(Object.keys(BUILTINS).sort())
    expect(SESSION_BUILTINS.filter((b) => b.background).map((b) => b.name).sort()).toEqual([...BACKGROUND_BUILTINS].sort())
  })

  it('offers each at its tier: the web ones outward, the ones that change background tasks write, CronList read', async () => {
    const { served } = await turnBuiltins(declared, [], { background: true })
    expect(Object.fromEntries(served.map((t) => [t.name, t.tier]))).toEqual({
      WebFetch: 'outward',
      WebSearch: 'outward',
      CronCreate: 'write',
      CronDelete: 'write',
      CronList: 'read',
      ScheduleWakeup: 'write',
      Monitor: 'write',
      Workflow: 'write',
      TaskStop: 'write',
    })
  })

  it('offers no background one where the session keeps no background tasks (a flow)', async () => {
    const { served } = await turnBuiltins(declared, [{ name: 'CronCreate', packages: ['p'] }], { background: false })
    expect(served.map((t) => t.name)).toEqual(['WebFetch', 'WebSearch'])
  })

  it("raises a Monitor by its source: ws outward, a command to Bash's tier, off without Bash", () => {
    expect(monitorAccess('write', { ws: { url: 'wss://x.example' } }, undefined)).toBe('outward')
    expect(monitorAccess('write', { command: 'ls' }, 'outward')).toBe('outward')
    expect(monitorAccess('write', { command: 'ls' }, 'read')).toBe('write')
    expect(monitorAccess('write', { command: 'ls' }, undefined)).toBe('off')
    expect(monitorAccess('write', { command: 'ls', ws: { url: 'wss://x.example' } }, 'read')).toBe('outward')
    expect(monitorAccess('write', {}, undefined)).toBe('outward')
    const tierOf = sessionTierOf([{ name: 'Monitor', tier: 'write' }, { name: 'CronList', tier: 'read' }], undefined)
    expect(tierOf('mcp__builtin__CronList', {})).toBe('read')
    expect(tierOf('mcp__builtin__Monitor', { command: 'ls' })).toBe('outward')
    expect(tierOf('mcp__builtin__WebFetch', {})).toBeUndefined()
    expect(sessionTierOf([{ name: 'Monitor', tier: 'write' }], 'write')('mcp__builtin__Monitor', { command: 'ls' })).toBe('write')
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
      ['Glob', { pattern: '**/*.scad' }],
      ['Glob', { pattern: 'src/{a,b}/*.ts' }],
      ['Glob', { pattern: 'notes/[ab].txt' }],
      ['Grep', { pattern: 'a', glob: '*.{ts,tsx}' }],
      ['Glob', { pattern: '**/*' }],
      ['Glob', { pattern: './notes/*.txt' }],
      ['Glob', { pattern: '{notes,src}/**/*.@(ts|scad)' }],
      ['Glob', { pattern: 'notes/[a-c]?.txt' }],
      ['Grep', { pattern: 'a' }],
      ['Grep', { pattern: 'a', path: 'notes' }],
      // Grep splits `glob` into several `rg --glob`s (see confine.ts); each piece is checked.
      ['Grep', { pattern: 'a', glob: '*.ts,*.tsx' }],
      ['Grep', { pattern: 'a', glob: '*.ts *.md' }],
      ['Grep', { pattern: 'a', glob: '*.{ts,tsx} *.md' }],
      // An escaped `(` and `)` name themselves.
      ['Glob', { pattern: 'file \\(1\\).txt' }],
      ['Grep', { pattern: 'a', glob: 'file \\(1\\).txt' }],
      // A `]` that closes no class is a name character, as minimatch and picomatch read it.
      ['Glob', { pattern: 'notes/a].txt' }],
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
      ['Glob', { pattern: '{..,x}/*' }],
      ['Glob', { pattern: 'notes/{x,..}/*' }],
      ['Glob', { pattern: '{.,..}/**/secret' }],
      ['Glob', { pattern: '@(..|x)/*' }],
      ['Glob', { pattern: '+(..)/secret' }],
      ['Glob', { pattern: '.{.,x}/*' }],
      ['Glob', { pattern: '.\\./*' }],
      ['Glob', { pattern: '[.][.]/*' }],
      ['Glob', { pattern: '@(.).' + '/*' }],
      ['Glob', { pattern: 'notes/.?/*' }],
      ['Grep', { pattern: 'kek', glob: '.{.,x}/*' }],
      ['Grep', { pattern: 'kek', glob: '[.][.]/secret' }],
      ['Glob', { pattern: '.{x,.}/*' }],
      ['Glob', { pattern: '.[x.]/*' }],
      ['Glob', { pattern: '.[!x]/*' }],
      ['Glob', { pattern: '.[^x]/*' }],
      ['Glob', { pattern: '.[-0]/*' }],
      ['Glob', { pattern: '.@(x|.)/*' }],
      ['Glob', { pattern: '.*(.)/*' }],
      ['Glob', { pattern: '.[[:punct:]]/*' }],
      ['Glob', { pattern: 'a/{x/..,y}/*' }],
      ['Glob', { pattern: '{-..0}/*' }],
      ['Grep', { pattern: 'kek', glob: '.{x,.}/*' }],
      ['Grep', { pattern: 'kek', glob: '.[x.]/*' }],
      ['Grep', { pattern: 'kek', glob: '.[!x]/*' }],
      ['Grep', { pattern: 'kek', glob: '.[^x]/*' }],
      ['Grep', { pattern: 'kek', glob: '.[-0]/*' }],
      ['Grep', { pattern: 'kek', glob: '.@(x|.)/*' }],
      ['Grep', { pattern: 'kek', glob: '.*(.)/*' }],
      ['Grep', { pattern: 'kek', glob: '.[[:punct:]]/*' }],
      ['Grep', { pattern: 'kek', glob: 'a/{x/..,y}/*' }],
      ['Grep', { pattern: 'kek', glob: '{-..0}/*' }],
      ['Grep', { pattern: 'kek', glob: 42 }],
      // What failing closed costs: no name in these is `..`, but each could match it.
      ['Glob', { pattern: '.*' }],
      ['Glob', { pattern: '*.*' }],
      ['Glob', { pattern: 'a..b*' }],
      ['Glob', { pattern: 'file (1).txt' }],
      ['Grep', { pattern: 'kek', glob: '.*' }],
      ['Grep', { pattern: 'kek', glob: '*.*' }],
      ['Grep', { pattern: 'kek', glob: 'a..b*' }],
      ['Grep', { pattern: 'kek', glob: 'file (1).txt' }],
      // Each piece Grep splits its glob into: on a comma, and on whitespace.
      ['Grep', { pattern: 'kek', glob: '.?,x' }],
      ['Grep', { pattern: 'kek', glob: 'x,.?/*' }],
      ['Grep', { pattern: 'kek', glob: '*.ts .?' }],
      ['Grep', { pattern: 'kek', glob: '*.ts\t.{x,.}/*' }],
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
      ['Grep', { pattern: 'k', glob: '*.ts ~/x' }],
      ['Grep', { pattern: 'k', glob: '*.ts,~/x' }],
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

describe("this session's tool-results directory (#5542)", () => {
  const SESSION = '1a2b3c4d-0000-4000-8000-00000000abcd'
  const OTHER = '9f8e7d6c-0000-4000-8000-00000000dcba'
  const setup = async () => {
    const paths = { stateDir: await mkdtemp(path.join(os.tmpdir(), 'results-state-')) }
    await ensureStateDirs(paths)
    const config = claudeConfigDir(paths)
    const root = await mkdtemp(path.join(os.tmpdir(), 'results.cwd_'))
    const project = path.join(config, 'projects', projectDirName(root))
    const dir = path.join(project, SESSION, 'tool-results')
    await mkdir(dir, { recursive: true })
    await writeFile(path.join(dir, 'toolu_1.json'), '{"big":true}')
    const other = path.join(project, OTHER, 'tool-results')
    await mkdir(other, { recursive: true })
    await writeFile(path.join(other, 'toolu_2.json'), '{"theirs":true}')
    await writeFile(path.join(project, `${SESSION}.jsonl`), '{"transcript":true}')
    await writeFile(path.join(config, '.credentials.json'), '{}')
    await writeFile(path.join(paths.stateDir, 'secret'), 'kek')
    return { paths, config, root, project, dir, other, guard: confineGuard(root, { configDir: config, sessionId: SESSION }) }
  }

  it('names the directory as Claude Code 2.1.314 does', () => {
    expect(projectDirName('/var/lib/scadbuddy-sessions/b1efa8b4-dd39-4462-ae9d-48097d63d68b')).toBe(
      '-var-lib-scadbuddy-sessions-b1efa8b4-dd39-4462-ae9d-48097d63d68b',
    )
    expect(projectDirName('/a.b/c_d/e f')).toBe('-a-b-c-d-e-f')
    // Over 200 characters: cut, then `-` and the base-36 hash of the whole path.
    const long = `/${'x'.repeat(250)}`
    expect(projectDirName(long)).toMatch(/^-x{199}-[0-9a-z]+$/)
    expect(projectDirName(long)).not.toBe(projectDirName(`/${'x'.repeat(251)}`))
    // Measured (#5588): the bundled Claude Code 2.1.314 (`claude --version`) ran `-p hi` with
    // CLAUDE_CONFIG_DIR set, a bogus API key and ANTHROPIC_BASE_URL at a closed port, in a
    // directory with this real path (221 characters). It made this directory under `projects/`
    // before the request failed.
    const measured = `/tmp/scadbuddy-5588/${'a'.repeat(100)}/${'b'.repeat(100)}`
    expect(projectDirName(measured)).toBe(`-tmp-scadbuddy-5588-${'a'.repeat(100)}-${'b'.repeat(79)}-e1mqbl`)
  })

  it('lets Read and Grep reach a file in it, and Grep the directory itself', async () => {
    const { dir, guard } = await setup()
    expect(await guard('Read', { file_path: path.join(dir, 'toolu_1.json') })).toBeUndefined()
    expect(await guard('Grep', { pattern: 'big', path: path.join(dir, 'toolu_1.json') })).toBeUndefined()
    expect(await guard('Grep', { pattern: 'big', path: dir })).toBeUndefined()
    expect(await guard('Grep', { pattern: 'big', path: `${dir}/` })).toBeUndefined()
  })

  it('refuses every tool that writes there, and Glob', async () => {
    const { dir, guard } = await setup()
    const file = path.join(dir, 'toolu_1.json')
    for (const [tool, input] of [
      ['Write', { file_path: file, content: 'x' }],
      ['Write', { file_path: path.join(dir, 'new.json'), content: 'x' }],
      ['Edit', { file_path: file, old_string: 'big', new_string: 'small' }],
      ['NotebookEdit', { notebook_path: path.join(dir, 'n.ipynb'), new_source: 'x' }],
      ['Glob', { pattern: '*.json', path: dir }],
      ['Glob', { pattern: `${dir}/*.json` }],
    ] as const) {
      expect(await guard(tool, input), `${tool} ${JSON.stringify(input)}`).toMatchObject({ deny: expect.stringContaining('outside it') })
    }
  })

  it("refuses another session's results, the transcripts, the config and the rest of the state dir", async () => {
    const { paths, config, project, other, guard } = await setup()
    for (const target of [
      path.join(other, 'toolu_2.json'),
      other,
      path.join(project, `${SESSION}.jsonl`),
      project,
      path.join(project, SESSION),
      path.join(config, '.credentials.json'),
      config,
      path.join(paths.stateDir, 'secret'),
      paths.stateDir,
    ]) {
      expect(await guard('Read', { file_path: target }), target).toMatchObject({ deny: expect.stringContaining('outside it') })
      expect(await guard('Grep', { pattern: 'x', path: target }), target).toMatchObject({ deny: expect.stringContaining('outside it') })
    }
  })

  it('refuses a climb with `..` and a link that points out', async () => {
    const { paths, project, dir, guard } = await setup()
    await symlink(path.join(paths.stateDir, 'secret'), path.join(dir, 'link.json'))
    await symlink(path.join(project, OTHER, 'tool-results'), path.join(dir, 'dirlink'))
    for (const target of [
      path.join(dir, 'link.json'),
      path.join(dir, 'dirlink', 'toolu_2.json'),
      path.join(dir, 'dirlink'),
      `${dir}/../../${OTHER}/tool-results/toolu_2.json`,
      `${dir}/../tool-results/toolu_1.json`,
      `${dir}/../../${SESSION}.jsonl`,
    ]) {
      expect(await guard('Read', { file_path: target }), target).toMatchObject({ deny: expect.stringContaining('outside it') })
      expect(await guard('Grep', { pattern: 'x', path: target }), target).toMatchObject({ deny: expect.stringContaining('outside it') })
    }
  })

  it('refuses it when tool-results itself is a link out', async () => {
    const { paths, config, root } = await setup()
    const session = '2b3c4d5e-0000-4000-8000-00000000abcd'
    const sessionDir = path.join(config, 'projects', projectDirName(root), session)
    await mkdir(sessionDir, { recursive: true })
    await symlink(paths.stateDir, path.join(sessionDir, 'tool-results'))
    const guard = confineGuard(root, { configDir: config, sessionId: session })
    expect(await guard('Read', { file_path: path.join(sessionDir, 'tool-results', 'secret') })).toMatchObject({
      deny: expect.stringContaining('outside it'),
    })
  })

  it('opens nothing without a session id, or with one that is not a UUID', async () => {
    const { config, root, project, dir } = await setup()
    expect(await confineGuard(root)('Read', { file_path: path.join(dir, 'toolu_1.json') })).toMatchObject({ deny: expect.stringContaining('outside it') })
    await mkdir(path.join(project, 'tool-results'), { recursive: true })
    const dot = confineGuard(root, { configDir: config, sessionId: '.' })
    expect(await dot('Read', { file_path: path.join(project, 'tool-results', 'x') })).toMatchObject({ deny: expect.stringContaining('outside it') })
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

  it("reads the source's rows once for a turn's built-ins", async () => {
    let reads = 0
    const counted = new AccessResolver({
      rows: (sources) => {
        reads += 1
        return Promise.resolve([row(CC, 'Grep', 'off')].filter((r) => sources.includes(r.source)))
      },
    })
    const { native: offered, served } = await turnBuiltins(counted, [], { background: true })
    expect(offered.map((t) => t.name)).not.toContain('Grep')
    expect(offered.map((t) => t.name)).toContain('Read')
    expect(served.map((t) => t.name)).toContain('WebFetch')
    expect(reads).toBe(1)
  })

  it('keeps it off for a package when the source is switched off, or its own row says off', async () => {
    expect(await resolver([row(CC, '*', 'off')]).native('Bash', 'off', ['shell'])).toBe('off')
    expect(await resolver([row(CC, '*', 'off')]).native('Read', 'read', ['shell'])).toBe('off')
    expect(await resolver([row(CC, 'Bash', 'off')]).native('Bash', 'off', ['shell'])).toBe('off')
  })
})
