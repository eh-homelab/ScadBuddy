import { mkdtempSync, rmSync } from 'node:fs'
import { writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { builtInFiles, readBuiltInFile } from '../src/plugins/packages/builtins.js'
import { fileContent, PREVIEW_CHARS } from '../src/plugins/packages/files.js'
import { PackageInstaller } from '../src/plugins/packages/install.js'
import { validateSource } from '../src/plugins/packages/source.js'
import type { PackagePin } from '../src/plugins/packages/store.js'
import { gitMissing, gitRepo, GREETER, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'

// Reading a plugin package's files for review (#1029): what one file's
// content looks like to Settings, the installer's verified copy for reading,
// and the built-ins' files.

describe('a file for review', () => {
  it('is text with its size, cut at the preview unless asked for all', () => {
    const small = fileContent('README.md', Buffer.from('# hi\n'), false)
    expect(small).toEqual({
      path: 'README.md',
      size: 5,
      binary: false,
      media_type: 'text/markdown',
      truncated: false,
      content: '# hi\n',
    })

    const big = Buffer.from('x'.repeat(PREVIEW_CHARS + 10))
    const cut = fileContent('notes.txt', big, false)
    expect(cut).toMatchObject({ truncated: true, size: PREVIEW_CHARS + 10, media_type: 'text/plain' })
    expect(cut.content).toHaveLength(PREVIEW_CHARS)
    expect(fileContent('notes.txt', big, true)).toMatchObject({ truncated: false, content: 'x'.repeat(PREVIEW_CHARS + 10) })
  })

  it('is a placeholder with its size and type when the bytes are not UTF-8 text', () => {
    const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0xff])
    expect(fileContent('icon.png', png, true)).toEqual({
      path: 'icon.png',
      size: 10,
      binary: true,
      media_type: 'image/png',
      truncated: false,
      content: null,
    })
    // A NUL byte is binary even when it decodes.
    expect(fileContent('data.bin', Buffer.from('a\0b'), false)).toMatchObject({
      binary: true,
      media_type: 'application/octet-stream',
    })
  })

  it('names the type of the files a plugin is made of', () => {
    const type = (p: string) => fileContent(p, Buffer.from('x'), false).media_type
    expect(type('.claude-plugin/plugin.json')).toBe('application/json')
    expect(type('skills/a/SKILL.md')).toBe('text/markdown')
    expect(type('model.scad')).toBe('text/x-openscad')
    expect(type('hooks/run.sh')).toBe('text/x-shellscript')
    expect(type('server.py')).toBe('text/x-python')
    expect(type('index.mjs')).toBe('text/javascript')
    expect(type('LICENSE')).toBe('text/plain')
  })
})

describe.skipIf(gitMissing !== undefined)(`reading an installed package's files${gitMissing ? ` (${gitMissing})` : ''}`, () => {
  let repos: Record<string, TestRepo>
  let cacheRoot: string
  let fetcher: ReturnType<typeof localFetcher>
  let installer: PackageInstaller

  beforeEach(() => {
    repos = {}
    cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-files-'))
    fetcher = localFetcher(repos)
    installer = new PackageInstaller({ fetcher, cacheRoot, resolve: resolver() })
  })
  afterEach(() => {
    for (const repo of Object.values(repos)) repo.remove()
    rmSync(cacheRoot, { recursive: true, force: true })
  })

  const install = async (): Promise<PackagePin> => {
    const p = await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/greeter.git' }))
    return { name: p.review.name, fetchUrl: p.fetchUrl, fetchPath: p.fetchPath, commit: p.commit, contentHash: p.contentHash }
  }

  it('reads from the verified copy, fetching the pin again when the cache is altered or gone', async () => {
    repos.greeter = gitRepo(GREETER)
    const pin = await install()
    expect((await installer.readFile(pin, 'README.md')).toString()).toBe('# greeter\n\nA fixture.\n')
    expect(fetcher.checkouts).toHaveLength(1)

    await writeFile(path.join(installer.cacheDir(pin), 'README.md'), 'altered\n')
    expect((await installer.readFile(pin, 'README.md')).toString()).toBe('# greeter\n\nA fixture.\n')
    rmSync(cacheRoot, { recursive: true, force: true })
    expect((await installer.readFile(pin, 'agents/helper.md')).toString()).toContain('name: helper')
    expect(fetcher.checkouts).toHaveLength(3)
  })

  it('reads a package the rules refuse, which is when it most needs reading', async () => {
    repos.greeter = gitRepo({ ...GREETER, 'skills/x/SKILL.md': '!`env`\n' })
    const pin = await install()
    await expect(installer.materialise(pin)).rejects.toThrow(/refused/)
    expect((await installer.readFile(pin, 'skills/x/SKILL.md')).toString()).toBe('!`env`\n')
  })

  it('never reads a pin whose files hash differently', async () => {
    repos.greeter = gitRepo(GREETER)
    const pin = await install()
    await expect(installer.readFile({ ...pin, contentHash: `sha256:${'0'.repeat(64)}` }, 'README.md')).rejects.toThrow(
      /not the pinned/,
    )
  })
})

describe("a built-in plugin's files", () => {
  it("lists ScadBuddy's own plugin's skills and subagents, with their sizes, and reads them", () => {
    const files = builtInFiles('scadbuddy')!
    const paths = files.map((f) => f.path)
    expect(paths).toContain('.claude-plugin/plugin.json')
    expect(paths).toContain('skills/authoring/SKILL.md')
    expect(paths.some((p) => p.startsWith('agents/'))).toBe(true)
    expect(files.every((f) => f.size > 0)).toBe(true)
    expect(readBuiltInFile('scadbuddy', 'skills/authoring/SKILL.md')!.toString()).toMatch(/^---/)
  })

  it('reads only a file it lists', () => {
    expect(readBuiltInFile('scadbuddy', '../package.json')).toBeUndefined()
    expect(readBuiltInFile('scadbuddy', '/etc/passwd')).toBeUndefined()
    expect(readBuiltInFile('greeter', 'README.md')).toBeUndefined()
    expect(builtInFiles('greeter')).toBeUndefined()
  })
})
