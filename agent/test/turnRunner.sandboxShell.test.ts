import { describe, expect, it } from 'vitest'
import { type AccessRow, BUILTIN_SOURCES } from '../src/access/model.js'
import { MONITOR_SHELL_OFF } from '../src/access/live.js'
import { OWN_PLUGIN_DIR } from '../src/harness/ownPlugin.js'
import type { PackagesForRun } from '../src/plugins/packages/install.js'
import { turnRunnerForTest } from './support/turnRunner.js'

// A turn's Bash and PowerShell where the deployment sets up sandbox pods (#5864): the session's
// own, on its `builtin` server, under the items of `builtin:claude-code`, never Claude Code's.

const row = (item: string, access: string): AccessRow => ({ source: BUILTIN_SOURCES.claudeCode, item, access, updated_by: 'browser:b' })
const sandbox = { acquire: () => Promise.reject(new Error('not called')) }

/** No package, or one allowed package that names Bash. */
function packages(namingBash: boolean): Omit<PackagesForRun, 'release'> {
  const loaded = namingBash ? [{ name: 'a', contentHash: 'hash-a', dir: '/plugins/a', allowed: true, builtinTools: ['Bash'] }] : []
  return { paths: [], allowedPaths: loaded.map((p) => p.dir), builtinTools: namingBash ? ['Bash'] : [], loaded, problems: [] }
}

async function build(options: { access?: AccessRow[]; namingBash?: boolean; sandboxed?: boolean; background?: boolean } = {}) {
  const rig = await turnRunnerForTest({
    ownPlugin: OWN_PLUGIN_DIR,
    packages: packages(options.namingBash ?? false),
    access: options.access ?? [],
    ...(options.background ? { background: true } : {}),
    ...(options.sandboxed === false ? {} : { sandbox }),
  })
  const turn = await rig.runner.assemble(rig.session, rig.principal)
  turn.release()
  return turn
}

describe("a turn's Bash and PowerShell in the session's sandbox (#5864)", () => {
  it("are served as the session's own, outward, and never native", async () => {
    const turn = await build()
    expect(turn.run.nativeTools?.map((t) => t.name)).not.toContain('Bash')
    expect(turn.run.nativeTools?.map((t) => t.name)).not.toContain('PowerShell')
    expect(Object.keys(turn.run.mcpServers ?? {})).toContain('builtin')
    expect(turn.fingerprintParts.servedBuiltins).toEqual(expect.arrayContaining(['Bash', 'PowerShell']))
    expect(await turn.run.live!.access('mcp__builtin__Bash', { command: 'ls' })).toBe('outward')
    expect(await turn.run.live!.access('mcp__builtin__PowerShell', { command: 'dir' })).toBe('outward')
  })

  it('follow their items in builtin:claude-code', async () => {
    const turn = await build({ access: [row('PowerShell', 'off'), row('Bash', 'write')] })
    expect(turn.fingerprintParts.servedBuiltins).toContain('Bash')
    expect(turn.fingerprintParts.servedBuiltins).not.toContain('PowerShell')
    expect(await turn.run.live!.access('mcp__builtin__Bash', { command: 'ls' })).toBe('write')
    expect(await turn.run.live!.access('mcp__builtin__PowerShell', { command: 'dir' })).toBe('off')
  })

  it('a package that names Bash gets the sandboxed one, not the native one', async () => {
    const turn = await build({ namingBash: true })
    expect(turn.run.nativeTools?.map((t) => t.name)).not.toContain('Bash')
    expect(turn.run.builtinTools ?? []).not.toContain('Bash')
    expect(turn.fingerprintParts.servedBuiltins).toEqual(expect.arrayContaining(['Bash', 'PowerShell']))
  })

  it("offers Monitor's command with a sandbox, at the sandboxed Bash's tier (#5916)", async () => {
    const turn = await build({ background: true })
    expect(turn.run.systemPromptAppend).toContain('A Monitor with a `ws` or a `command` source asks the user first (Bash is outward here).')
    expect(await turn.run.live!.access('mcp__builtin__Monitor', { description: 'x', command: 'tail -f x' })).toBe('outward')
  })

  it("refuses Monitor's command with a sandbox while the sandboxed Bash is off", async () => {
    const turn = await build({ background: true, access: [row('Bash', 'off')] })
    expect(turn.run.systemPromptAppend).toContain("its `command` source needs Bash, which is off here")
    expect(await turn.run.live!.access('mcp__builtin__Monitor', { description: 'x', command: 'tail -f x' })).toEqual({ off: MONITOR_SHELL_OFF })
  })

  it('without a sandbox nothing changes', async () => {
    const turn = await build({ sandboxed: false })
    expect(turn.fingerprintParts.servedBuiltins).not.toContain('Bash')
    expect(turn.fingerprintParts.servedBuiltins).not.toContain('PowerShell')
    expect(turn.run.nativeTools?.map((t) => t.name)).not.toContain('Bash')
  })
})
