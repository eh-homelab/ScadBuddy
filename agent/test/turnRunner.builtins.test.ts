import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { type AccessRow, BUILTIN_SOURCES, SOURCE_ITEM } from '../src/access/model.js'
import { MONITOR_SHELL_NEXT_TURN, MONITOR_SHELL_OFF } from '../src/access/live.js'
import { hashTree } from '../src/plugins/packages/hash.js'
import type { LoadedPackage, PackagesForRun } from '../src/plugins/packages/install.js'
import { turnRunnerForTest } from './support/turnRunner.js'

// Every session has every built-in (#2726 R2, #4773): the session's own WebFetch, WebSearch
// and background ones are offered in every turn, each by its item under `builtin:claude-code`,
// not only when an allowed plugin package names them (found in prod on 2026-11-10).

const CC = BUILTIN_SOURCES.claudeCode
const row = (item: string, access: string): AccessRow => ({ source: CC, item, access, updated_by: 'browser:b' })
const NONE: Omit<PackagesForRun, 'release'> = { paths: [], allowedPaths: [], builtinTools: [], loaded: [], problems: [] }
const ALL = ['WebFetch', 'WebSearch', 'CronCreate', 'CronDelete', 'CronList', 'ScheduleWakeup', 'Monitor', 'Workflow', 'TaskStop']

async function allowed(name: string, builtins: string[]): Promise<Omit<PackagesForRun, 'release'>> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'runner-builtins-'))
  await mkdir(path.join(dir, '.claude-plugin'))
  await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name }))
  await mkdir(path.join(dir, 'skills', 'jot'), { recursive: true })
  await writeFile(path.join(dir, 'skills', 'jot', 'SKILL.md'), `---\nname: jot\ndescription: jots\nallowed-tools: ${builtins.join(', ')}\n---\nJot.\n`)
  const pkg: LoadedPackage = { name, contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: builtins }
  return { paths: [], allowedPaths: [dir], builtinTools: builtins, loaded: [pkg], problems: [] }
}

async function assemble(options: { access?: AccessRow[]; background?: boolean; backgroundTools?: boolean; packages?: Omit<PackagesForRun, 'release'> } = {}) {
  const rig = await turnRunnerForTest({
    packages: options.packages ?? NONE,
    access: options.access ?? [],
    background: options.background ?? true,
    ...(options.backgroundTools !== undefined ? { backgroundTools: options.backgroundTools } : {}),
  })
  const turn = await rig.runner.assemble(rig.session, rig.principal)
  turn.release()
  return turn
}

describe("the session's own built-ins in every turn", () => {
  it('a turn with no packages offers every one, at its tier', async () => {
    const turn = await assemble()
    expect(turn.fingerprintParts.servedBuiltins).toEqual(ALL)
    expect(Object.keys(turn.run.mcpServers ?? {})).toContain('builtin')
    // Served by the session, never handed to Claude Code as its own.
    expect(turn.run.builtinTools ?? []).toEqual([])
    const live = turn.run.live!.access
    expect(await live('mcp__builtin__WebFetch', { url: 'https://example.com', prompt: 'p' })).toBe('outward')
    expect(await live('mcp__builtin__WebSearch', { query: 'q' })).toBe('outward')
    expect(await live('mcp__builtin__CronCreate', { cron: '1 * * * *', prompt: 'p' })).toBe('write')
    expect(await live('mcp__builtin__CronList', {})).toBe('read')
    expect(await live('mcp__builtin__TaskStop', { task_id: 't' })).toBe('write')
    expect(turn.eventTierOf('mcp__builtin__CronList', {})).toBe('read')
    expect(turn.eventTierOf('mcp__builtin__WebFetch', {})).toBe('outward')
    // The model is told they are there, and which ask first, with no word of a package.
    const note = turn.run.systemPromptAppend ?? ''
    expect(note).toContain('ScheduleWakeup (mcp__builtin__ScheduleWakeup)')
    expect(note).toContain('A call of WebFetch, WebSearch asks the user first.')
    expect(note).not.toContain('plugin package you have')
  })

  it("a person's off removes one, and wins over a package that names it", async () => {
    const turn = await assemble({ access: [row('ScheduleWakeup', 'off')] })
    expect(turn.fingerprintParts.servedBuiltins).not.toContain('ScheduleWakeup')
    expect(turn.fingerprintParts.servedBuiltins).toContain('CronCreate')
    expect(await turn.run.live!.access('mcp__builtin__ScheduleWakeup', { delaySeconds: 60 })).toBe('off')
    const named = await assemble({ access: [row('WebFetch', 'off')], packages: await allowed('fetcher', ['WebFetch']) })
    expect(named.fingerprintParts.servedBuiltins).not.toContain('WebFetch')
    // Nor Claude Code's own in its place.
    expect(named.run.builtinTools ?? []).not.toContain('WebFetch')
    // The source switched off removes them all.
    const off = await assemble({ access: [{ source: CC, item: SOURCE_ITEM, access: 'off', updated_by: 'browser:b' }] })
    expect(off.fingerprintParts.servedBuiltins).toEqual([])
    expect(Object.keys(off.run.mcpServers ?? {})).not.toContain('builtin')
  })

  it("an off switched while the turn runs refuses the call", async () => {
    const rig = await turnRunnerForTest({ packages: NONE, background: true })
    const turn = await rig.runner.assemble(rig.session, rig.principal)
    turn.release()
    await rig.access.setItem(CC, 'CronCreate', 'off', { actor: { kind: 'browser', id: 'b' } } as never)
    expect(await turn.run.live!.access('mcp__builtin__CronCreate', { cron: '1 * * * *', prompt: 'p' })).toBe('off')
  })

  it('an agent with no background tasks offers WebFetch and WebSearch only', async () => {
    const turn = await assemble({ background: false })
    expect(turn.fingerprintParts.servedBuiltins).toEqual(['WebFetch', 'WebSearch'])
    expect(turn.run.systemPromptAppend).not.toContain('The session keeps the jobs')
    // A package that names a background one gets neither the session's nor Claude Code's.
    const named = await assemble({ background: false, packages: await allowed('loop', ['ScheduleWakeup']) })
    expect(named.fingerprintParts.servedBuiltins).toEqual(['WebFetch', 'WebSearch'])
    expect(named.run.builtinTools ?? []).toEqual([])
  })

  it('Monitor watches a WebSocket, asking first, and runs a command only while Bash is on', async () => {
    const turn = await assemble()
    const live = turn.run.live!.access
    expect(await live('mcp__builtin__Monitor', { description: 'd', ws: { url: 'wss://example.com' } })).toBe('outward')
    expect(await live('mcp__builtin__Monitor', { description: 'd', command: 'ls' })).toEqual({ off: MONITOR_SHELL_OFF })
    expect(turn.eventTierOf('mcp__builtin__Monitor', { description: 'd', ws: { url: 'wss://example.com' } })).toBe('outward')
    expect(turn.run.systemPromptAppend).toContain('its `command` source needs Bash, which is off here')
    // A package switches Bash on, each call asking first: a Monitor command then too.
    const shell = await assemble({ packages: await allowed('shell', ['Bash']) })
    expect(shell.run.nativeTools?.find((t) => t.name === 'Bash')).toEqual({ name: 'Bash', tier: 'outward' })
    expect(await shell.run.live!.access('mcp__builtin__Monitor', { description: 'd', command: 'ls' })).toBe('outward')
    expect(shell.eventTierOf('mcp__builtin__Monitor', { description: 'd', command: 'ls' })).toBe('outward')
    expect(shell.run.systemPromptAppend).not.toContain('which is off here')
  })

  it("a flow-started session's turn, or a Workflow agent's chat's, offers WebFetch and WebSearch only", async () => {
    // ClaimedSession.backgroundTools is false for both (SessionManager.start startedByFlow, startChild).
    const turn = await assemble({ backgroundTools: false })
    expect(turn.fingerprintParts.servedBuiltins).toEqual(['WebFetch', 'WebSearch'])
    expect(turn.run.systemPromptAppend).not.toContain('The session keeps the jobs')
    const named = await assemble({ backgroundTools: false, packages: await allowed('loop', ['Workflow', 'CronCreate']) })
    expect(named.fingerprintParts.servedBuiltins).toEqual(['WebFetch', 'WebSearch'])
    expect(named.run.builtinTools ?? []).toEqual([])
  })

  it('runs a Monitor command at write when an admin set Bash on at write', async () => {
    const turn = await assemble({ access: [row('Bash', 'write')] })
    expect(turn.run.nativeTools?.find((t) => t.name === 'Bash')).toEqual({ name: 'Bash', tier: 'write' })
    expect(await turn.run.live!.access('mcp__builtin__Monitor', { description: 'd', command: 'ls' })).toBe('write')
    expect(turn.eventTierOf('mcp__builtin__Monitor', { description: 'd', command: 'ls' })).toBe('write')
    // A WebSocket still asks first.
    expect(await turn.run.live!.access('mcp__builtin__Monitor', { description: 'd', ws: { url: 'wss://example.com' } })).toBe('outward')
  })

  it("answers off for one of the session's own the turn does not serve, even when a package names it", async () => {
    const turn = await assemble({ access: [row('WebFetch', 'off')], packages: await allowed('fetcher', ['WebFetch']) })
    expect(await turn.run.live!.access('mcp__builtin__WebFetch', { url: 'https://example.com', prompt: 'p' })).toBe('off')
    const none = await assemble({ background: false })
    expect(await none.run.live!.access('mcp__builtin__CronCreate', { cron: '1 * * * *', prompt: 'p' })).toBe('off')
    const flow = await assemble({ backgroundTools: false })
    expect(await flow.run.live!.access('mcp__builtin__ScheduleWakeup', { delaySeconds: 60 })).toBe('off')
  })

  it('refuses a Monitor command, never asking, when Bash is switched on after the turn was assembled', async () => {
    const rig = await turnRunnerForTest({ packages: NONE, background: true })
    const turn = await rig.runner.assemble(rig.session, rig.principal)
    turn.release()
    // Its server has no command queue: an approval would only run a call certain to fail.
    await rig.access.setItem(CC, 'Bash', 'write', { actor: { kind: 'browser', id: 'b' } } as never)
    expect(await turn.run.live!.access('mcp__builtin__Monitor', { description: 'd', command: 'ls' })).toEqual({ off: MONITOR_SHELL_NEXT_TURN })
  })
})
