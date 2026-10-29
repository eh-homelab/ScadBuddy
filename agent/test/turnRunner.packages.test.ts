import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { type AccessRow, BUILTIN_SOURCES, packageServerSource, packageSource, SOURCE_ITEM } from '../src/access/model.js'
import { declaredConfigs } from '../src/harness/plugins.js'
import { OWN_PLUGIN_DIR } from '../src/harness/ownPlugin.js'
import { hashTree } from '../src/plugins/packages/hash.js'
import type { LoadedPackage, PackagesForRun } from '../src/plugins/packages/install.js'
import { packageItems } from '../src/plugins/packages/vet.js'
import { turnRunnerForTest } from './support/turnRunner.js'
import type { AccessReader } from '../src/access/store.js'
import { TurnAssemblyError } from '../src/sessions/turnRunner.js'
import { readdir } from 'node:fs/promises'

// A classic turn's packages and own plugin (#4757): each loaded as its derived copy, without
// what Settings → Tools & plugins switched off; the allowed built-ins by their access.

const BY = 'browser:b'
const row = (source: string, item: string, access: string): AccessRow => ({ source, item, access, updated_by: BY })

type Spec = { name: string; allowed?: boolean; skills?: string[]; builtins?: string[]; hooks?: unknown; mcp?: unknown; agents?: Record<string, string> }

async function pkg(spec: Spec): Promise<LoadedPackage> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'runner-pkg-'))
  await mkdir(path.join(dir, '.claude-plugin'))
  await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: spec.name }))
  for (const skill of spec.skills ?? ['jot']) {
    await mkdir(path.join(dir, 'skills', skill), { recursive: true })
    const tools = spec.builtins?.length ? `allowed-tools: ${spec.builtins.join(', ')}\n` : ''
    await writeFile(path.join(dir, 'skills', skill, 'SKILL.md'), `---\nname: ${skill}\ndescription: ${skill}s\n${tools}---\n${skill}.\n`)
  }
  for (const [file, text] of Object.entries(spec.agents ?? {})) {
    await mkdir(path.dirname(path.join(dir, 'agents', file)), { recursive: true })
    await writeFile(path.join(dir, 'agents', file), text)
  }
  if (spec.hooks) {
    await mkdir(path.join(dir, 'hooks'))
    await writeFile(path.join(dir, 'hooks', 'hooks.json'), JSON.stringify(spec.hooks))
  }
  if (spec.mcp) await writeFile(path.join(dir, '.mcp.json'), JSON.stringify(spec.mcp))
  return { name: spec.name, contentHash: (await hashTree(dir)).hash, dir, allowed: spec.allowed ?? false, builtinTools: spec.builtins ?? [] }
}

function forRun(loaded: LoadedPackage[]): Omit<PackagesForRun, 'release'> {
  return {
    paths: loaded.filter((p) => !p.allowed).map((p) => p.dir),
    allowedPaths: loaded.filter((p) => p.allowed).map((p) => p.dir),
    builtinTools: [...new Set(loaded.filter((p) => p.allowed).flatMap((p) => p.builtinTools))].sort(),
    loaded,
    problems: [],
  }
}

async function assemble(loaded: LoadedPackage[], access: AccessRow[] = [], ownPlugin = OWN_PLUGIN_DIR, reader?: (base: AccessReader) => AccessReader) {
  const rig = await turnRunnerForTest({ ownPlugin, packages: forRun(loaded), access, ...(reader ? { reader } : {}) })
  const turn = await rig.runner.assemble(rig.session, rig.principal)
  turn.release()
  return { turn, ...rig }
}

/** The one loaded copy of `p` among the turn's plugin paths. */
function copyOf(dirs: readonly string[] | undefined, p: LoadedPackage): string {
  const found = (dirs ?? []).filter((d) => d === p.dir || path.basename(d) === p.name)
  expect(found).toHaveLength(1)
  return found[0]!
}

describe("a classic turn's packages and own plugin (#4757)", () => {
  it('a disabled skill is absent from the package Claude Code loads', async () => {
    const notes = await pkg({ name: 'notes', skills: ['jot', 'wipe'] })
    const { turn } = await assemble([notes], [row(packageSource('notes'), 'skill:wipe', 'off')])
    const dir = copyOf(turn.run.pluginPaths, notes)
    expect(dir).not.toBe(notes.dir)
    expect(existsSync(path.join(dir, 'skills', 'wipe'))).toBe(false)
    expect(existsSync(path.join(dir, 'skills', 'jot', 'SKILL.md'))).toBe(true)
    // Named by its own hash, which the warm cache fingerprints.
    const hash = (await hashTree(dir)).hash
    expect(path.basename(path.dirname(dir))).toBe(hash.replace(/^sha256:/, ''))
    expect(turn.fingerprintParts.packageHashes).toEqual([hash])
    expect(existsSync(path.join(notes.dir, 'skills', 'wipe'))).toBe(true)
  })

  it('a disabled hook is absent from the hooks Claude Code loads', async () => {
    const hooks = {
      hooks: {
        Stop: [{ hooks: [{ type: 'http', url: 'https://hooks.example/stop' }] }],
        PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'http', url: 'https://hooks.example/pre' }] }],
      },
    }
    const notes = await pkg({ name: 'notes', hooks })
    const stop = packageItems(notes.dir, declaredConfigs(notes.dir).manifest, notes.name).hooks.find((h) => h.event === 'Stop')!
    const { turn } = await assemble([notes], [row(packageSource('notes'), `hook:Stop:${stop.id}`, 'off')])
    const dir = copyOf(turn.run.pluginPaths, notes)
    const loaded = JSON.parse(await readFile(path.join(dir, 'hooks', 'hooks.json'), 'utf8')) as { hooks: Record<string, unknown> }
    expect(Object.keys(loaded.hooks)).toEqual(['PreToolUse'])
  })

  it('the own plugin is loaded as a copy without its disabled subagent', async () => {
    const { turn } = await assemble([], [row(BUILTIN_SOURCES.ownPlugin, 'subagent:print-analyst', 'off'), row(BUILTIN_SOURCES.ownPlugin, 'skill:print', 'off')])
    const own = turn.run.ownPlugin!
    expect(own).not.toBe(OWN_PLUGIN_DIR)
    expect(path.basename(own)).toBe('scadbuddy')
    expect(existsSync(path.join(own, 'agents', 'print-analyst.md'))).toBe(false)
    expect(existsSync(path.join(own, 'agents', 'model-author.md'))).toBe(true)
    expect(existsSync(path.join(own, 'skills', 'print'))).toBe(false)
    expect(existsSync(path.join(own, 'skills', 'authoring', 'SKILL.md'))).toBe(true)
    // A call naming it is refused too, mid-turn as at its start.
    expect(await turn.run.live!.access('Agent', { subagent_type: 'scadbuddy:print-analyst' })).toEqual({
      off: `subagent "print-analyst" of ScadBuddy's own plugin is switched off in Settings → Tools & plugins; the user can turn it back on there`,
    })
    expect(await turn.run.live!.access('Agent', { subagent_type: 'scadbuddy:model-author' })).toBeUndefined()
    // Fingerprinted by what was loaded, so a warm process is not reused across the change.
    const plain = await assemble([])
    expect(turn.fingerprintParts.ownPlugin).not.toEqual(plain.turn.fingerprintParts.ownPlugin)
  })

  it('nothing off loads the package and the own plugin as they are', async () => {
    const notes = await pkg({ name: 'notes' })
    const { turn } = await assemble([notes], [row(packageSource('notes'), 'skill:jot', 'on')])
    expect(turn.run.pluginPaths).toContain(notes.dir)
    expect(turn.run.ownPlugin).toBe(OWN_PLUGIN_DIR)
    expect(turn.fingerprintParts.packageHashes).toEqual([notes.contentHash])
  })

  it('one package off, one write: Bash offered in neither runtime', async () => {
    // The durable half is turnPackages.test.ts's, over the same rows.
    const a = await pkg({ name: 'a', allowed: true, builtins: ['Bash'] })
    const b = await pkg({ name: 'b', allowed: true, builtins: ['Bash', 'Grep'] })
    const { turn } = await assemble([a, b], [row(packageSource('a'), 'builtin:Bash', 'write'), row(packageSource('b'), 'builtin:Bash', 'off')])
    expect(turn.run.builtinTools).toEqual(['Grep'])
    expect(await turn.run.live!.access('Bash', { command: 'ls' })).toBe('off')
    // Unset in both: offered, each call outward; write in both: write.
    const unset = await assemble([a, b])
    expect(unset.turn.run.builtinTools).toEqual(['Bash', 'Grep'])
    expect(await unset.turn.run.live!.access('Bash', { command: 'ls' })).toBe('outward')
    const write = await assemble([a, b], [row(packageSource('a'), 'builtin:Bash', 'write'), row(packageSource('b'), 'builtin:Bash', 'write')])
    expect(await write.turn.run.live!.access('Bash', { command: 'ls' })).toBe('write')
  })

  it('a local server switched off is not in the copy, and an off tool is disallowed', async () => {
    const mcp = { mcpServers: { local: { command: 'node', args: ['l.mjs'] }, other: { command: 'node', args: ['o.mjs'] } } }
    const notes = await pkg({ name: 'notes', allowed: true, mcp })
    const { turn, access } = await assemble([notes], [
      row(packageServerSource('notes', 'local'), SOURCE_ITEM, 'off'),
      row(packageServerSource('notes', 'other'), 'wipe', 'off'),
      row(packageServerSource('notes', 'other'), 'look', 'read'),
    ])
    const dir = copyOf(turn.run.allowedPluginPaths, notes)
    const servers = JSON.parse(await readFile(path.join(dir, '.mcp.json'), 'utf8')) as { mcpServers: Record<string, unknown> }
    expect(Object.keys(servers.mcpServers)).toEqual(['other'])
    expect(turn.run.disallowedTools).toEqual(['mcp__plugin_notes_other__wipe'])
    expect(turn.fingerprintParts.disabledTools).toContain('mcp__plugin_notes_other__wipe')
    // Its tools take their stored access per call, the switch too.
    const live = turn.run.live!
    expect(await live.access('mcp__plugin_notes_other__look')).toBe('read')
    expect(await live.access('mcp__plugin_notes_other__new')).toBe('outward')
    expect(await live.access('mcp__plugin_notes_other__wipe')).toBe('off')
    await access.setEnabled(packageServerSource('notes', 'other'), false)
    expect(await live.access('mcp__plugin_notes_other__look')).toBe('off')
  })

  it("leaves out the local servers whose names another package's server carries", async () => {
    // `a.b`'s `s` and `a_b`'s `s` are both plugin_a_b_s: neither is started.
    const mcp = { mcpServers: { s: { command: 'node' }, own: { command: 'node' } } }
    const ab = await pkg({ name: 'a.b', allowed: true, mcp })
    const a_b = await pkg({ name: 'a_b', allowed: true, mcp: { mcpServers: { s: { command: 'node' } } } })
    const { turn } = await assemble([ab, a_b])
    const first = JSON.parse(await readFile(path.join(copyOf(turn.run.allowedPluginPaths, ab), '.mcp.json'), 'utf8')) as { mcpServers: Record<string, unknown> }
    const second = JSON.parse(await readFile(path.join(copyOf(turn.run.allowedPluginPaths, a_b), '.mcp.json'), 'utf8')) as { mcpServers: Record<string, unknown> }
    expect(Object.keys(first.mcpServers)).toEqual(['own'])
    expect(Object.keys(second.mcpServers)).toEqual([])
    expect(await turn.run.live!.access('mcp__plugin_a_b_s__x')).toBeUndefined()
  })

  it("takes a package's items from the loaded pin: its root skill by the pin's name, its aliases from its files", async () => {
    // A package that is one skill: its key is the pin's name, whatever a stored review once said.
    const dir = await mkdtemp(path.join(os.tmpdir(), 'runner-solo-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'solo' }))
    await writeFile(path.join(dir, 'SKILL.md'), '---\ndescription: solo\n---\nSolo.\n')
    const solo: LoadedPackage = { name: 'solo', contentHash: (await hashTree(dir)).hash, dir, allowed: false, builtinTools: [] }
    const review = await pkg({ name: 'review', agents: { 'review/deep.md': '---\nname: deep-review\ndescription: deep\n---\nDeep.\n' } })
    const { turn } = await assemble([solo, review], [row(packageSource('solo'), 'skill:solo', 'off'), row(packageSource('review'), 'subagent:review:deep', 'off')])
    expect(existsSync(path.join(copyOf(turn.run.pluginPaths, solo), 'SKILL.md'))).toBe(false)
    expect(await turn.run.live!.access('Skill', { skill: 'solo:solo' })).toMatchObject({ off: expect.stringContaining('skill "solo"') })
    expect(await turn.run.live!.access('Agent', { subagent_type: 'review:deep-review' })).toMatchObject({ off: expect.stringContaining('subagent "review:deep"') })
    expect(await turn.run.live!.access('Agent', { subagent_type: 'review:review:deep' })).toMatchObject({ off: expect.stringContaining('subagent "review:deep"') })
  })

  it('leaves out a local server whose tools would carry a name with __, and says so as a durable turn does', async () => {
    const notes = await pkg({ name: 'notes', allowed: true, mcp: { mcpServers: { x__y: { command: 'node' }, ok: { command: 'node' } } } })
    const { turn, appended } = await assemble([notes], [row(packageServerSource('notes', 'x__y'), 'run', 'off')])
    const servers = JSON.parse(await readFile(path.join(copyOf(turn.run.allowedPluginPaths, notes), '.mcp.json'), 'utf8')) as { mcpServers: Record<string, unknown> }
    expect(Object.keys(servers.mcpServers)).toEqual(['ok'])
    expect(JSON.stringify(appended)).toContain('plugin package notes: MCP server \\"x__y\\" has a name that cannot be offered')
  })

  it('loads nothing when what is off cannot be read', async () => {
    const notes = await pkg({ name: 'notes', skills: ['jot', 'wipe'] })
    const failing = (base: AccessReader): AccessReader => ({
      rows: (sources) => (sources.some((s) => s.startsWith('package:')) ? Promise.reject(new Error('db down')) : base.rows(sources)),
    })
    const rig = await turnRunnerForTest({ ownPlugin: OWN_PLUGIN_DIR, packages: forRun([notes]), access: [row(packageSource('notes'), 'skill:wipe', 'off')], reader: failing })
    await expect(rig.runner.assemble(rig.session, rig.principal)).rejects.toBeInstanceOf(TurnAssemblyError)
    expect(await readdir(path.join(rig.paths.stateDir, 'plugins-local')).catch(() => [])).toEqual([])
  })

  it('does not load the own plugin when its copy cannot be made, and says so', async () => {
    // A name that is not a safe path segment: its copy cannot be placed.
    const own = await mkdtemp(path.join(os.tmpdir(), 'runner-own-'))
    await mkdir(path.join(own, '.claude-plugin'))
    await writeFile(path.join(own, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'not safe/name' }))
    await mkdir(path.join(own, 'skills', 'print'), { recursive: true })
    await writeFile(path.join(own, 'skills', 'print', 'SKILL.md'), '---\nname: print\ndescription: prints\n---\nPrint.\n')
    const { turn, appended } = await assemble([], [row(BUILTIN_SOURCES.ownPlugin, 'skill:print', 'off')], own)
    expect(turn.run.ownPlugin).toBeUndefined()
    expect(turn.fingerprintParts.ownPlugin).toBeNull()
    expect(JSON.stringify(appended)).toContain("ScadBuddy's own plugin was not loaded")
  })
})
