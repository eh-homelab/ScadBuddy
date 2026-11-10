import { describe, expect, it } from 'vitest'
import { type AccessRow, BUILTIN_SOURCES } from '../src/access/model.js'
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

async function build(options: { access?: AccessRow[]; namingBash?: boolean; sandboxed?: boolean } = {}) {
  const rig = await turnRunnerForTest({
    ownPlugin: OWN_PLUGIN_DIR,
    packages: packages(options.namingBash ?? false),
    access: options.access ?? [],
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
    expect(turn.fingerprintParts.servedBuiltins).toEqual(['Bash', 'PowerShell'])
    expect(await turn.run.live!.access('mcp__builtin__Bash', { command: 'ls' })).toBe('outward')
    expect(await turn.run.live!.access('mcp__builtin__PowerShell', { command: 'dir' })).toBe('outward')
  })

  it('follow their items in builtin:claude-code', async () => {
    const turn = await build({ access: [row('PowerShell', 'off'), row('Bash', 'write')] })
    expect(turn.fingerprintParts.servedBuiltins).toEqual(['Bash'])
    expect(await turn.run.live!.access('mcp__builtin__Bash', { command: 'ls' })).toBe('write')
    expect(await turn.run.live!.access('mcp__builtin__PowerShell', { command: 'dir' })).toBe('off')
  })

  it('a package that names Bash gets the sandboxed one, not the native one', async () => {
    const turn = await build({ namingBash: true })
    expect(turn.run.nativeTools?.map((t) => t.name)).not.toContain('Bash')
    expect(turn.run.builtinTools ?? []).not.toContain('Bash')
    expect(turn.fingerprintParts.servedBuiltins).toEqual(['Bash', 'PowerShell'])
  })

  it('without a sandbox nothing changes', async () => {
    const turn = await build({ sandboxed: false })
    expect(turn.fingerprintParts.servedBuiltins).toEqual([])
    expect(turn.run.nativeTools?.map((t) => t.name)).not.toContain('Bash')
  })
})
