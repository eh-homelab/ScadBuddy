import { describe, expect, it } from 'vitest'
import { BUILTIN_STRICTNESS, builtinItem, hookItem, itemKindOf, pluginOfName, skillItem, strictest, subagentItem } from '../src/access/items.js'
import { MIGRATED_BY } from '../src/access/model.js'
import { AccessResolver } from '../src/access/resolver.js'
import type { AccessReader } from '../src/access/store.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { memoryAccess, rowsFromOverrides } from './support/memoryAccess.js'

const S = 'builtin:scadbuddy'
const B = 'builtin:browser'
const BY = 'browser:browser'
const BROWSER = ALL_TOOLS.filter((t) => t.name.startsWith('browser_')).map((t) => t.name)

describe('AccessResolver', () => {
  it('answers defaults without a reader', async () => {
    const resolver = new AccessResolver(undefined)
    expect(await resolver.registry()).toEqual({ tool_tiers: {}, disabled_tools: [] })
    expect(await resolver.registryOverrides().of('send_to_bambuddy')).toEqual({ disabled: false })
    expect(await resolver.registryOverrides().disabled()).toEqual(new Set())
    expect(await resolver.plugin('remote:hs')).toEqual({ toolTiers: {}, disabledTools: [] })
    expect(await resolver.enabled('builtin:http')).toBe(true)
    expect(await resolver.enabled('remote:hs')).toBe(false)
    expect(await resolver.item('remote:hs', 'search', 'outward')).toBe('outward')
  })

  it('plugin() keeps a tool named __proto__ as its own key', async () => {
    const { access } = memoryAccess([
      { source: 'remote:hs', item: '__proto__', access: 'read', updated_by: 'browser:browser' },
      { source: 'remote:hs', item: 'search', access: 'off', updated_by: 'browser:browser' },
    ])
    const { toolTiers, disabledTools } = await new AccessResolver(access).plugin('remote:hs')
    expect(Object.hasOwn(toolTiers, '__proto__')).toBe(true)
    expect(toolTiers['__proto__']).toBe('read')
    expect(Object.keys(toolTiers)).toEqual(['__proto__'])
    expect(disabledTools).toEqual(['search'])
  })

  it('registryOverrides reads once per call and answers a lowered tier', async () => {
    const { access, reads } = memoryAccess(rowsFromOverrides(S, { send_to_bambuddy: 'write' }, []))
    const overrides = new AccessResolver(access).registryOverrides()
    expect(reads.n).toBe(0)
    expect(await overrides.of('send_to_bambuddy')).toEqual({ tier: 'write', disabled: false })
    expect(reads.n).toBe(1)
    expect(await overrides.of('get_model')).toEqual({ disabled: false })
    expect(reads.n).toBe(2)
  })

  it('registryOverrides().disabled() lists every off registry tool', async () => {
    const { access } = memoryAccess([
      ...rowsFromOverrides(S, {}, ['send_to_bambuddy', 'get_model']),
      ...rowsFromOverrides('builtin:browser', {}, ['browser_click']),
      // Not a registry tool, and not this source's.
      ...rowsFromOverrides(S, {}, ['no_such_tool']),
      ...rowsFromOverrides('remote:hs', {}, ['search']),
    ])
    expect(await new AccessResolver(access).registryOverrides().disabled()).toEqual(new Set(['browser_click', 'get_model', 'send_to_bambuddy']))
  })

  it('applies the migrated rule to registry tools only', async () => {
    const { access } = memoryAccess([
      { source: S, item: 'send_to_bambuddy', access: 'write', updated_by: MIGRATED_BY },
      { source: 'remote:hs', item: 'search', access: 'read', updated_by: MIGRATED_BY },
    ])
    const resolver = new AccessResolver(access, ALL_TOOLS)
    expect(await resolver.item(S, 'send_to_bambuddy', 'outward')).toBe('outward')
    expect(await resolver.item('remote:hs', 'search', 'outward')).toBe('read')
    expect(await resolver.plugin('remote:hs')).toEqual({ toolTiers: { search: 'read' }, disabledTools: [] })
  })

  it('item() answers outward for an unknown remote tool and the stored access for a known one', async () => {
    const { access } = memoryAccess(rowsFromOverrides('remote:hs', { search: 'read' }, ['delete']))
    const resolver = new AccessResolver(access)
    expect(await resolver.item('remote:hs', 'unknown', 'outward')).toBe('outward')
    expect(await resolver.item('remote:hs', 'search', 'outward')).toBe('read')
    expect(await resolver.item('remote:hs', 'delete', 'outward')).toBe('off')
  })

  it('enabled() reads the source row', async () => {
    const { access } = memoryAccess([
      { source: 'builtin:http', item: '*', access: 'off', updated_by: 'browser:browser' },
      { source: 'remote:hs', item: '*', access: 'on', updated_by: 'browser:browser' },
    ])
    const resolver = new AccessResolver(access)
    expect(await resolver.enabled('builtin:http')).toBe(false)
    expect(await resolver.enabled('remote:hs')).toBe(true)
    expect(await resolver.enabled('remote:other')).toBe(false)
  })

  it('entry() answers the switch and the stored access in one read, undefined where none is stored', async () => {
    const { access, reads } = memoryAccess([
      { source: 'builtin:http', item: '*', access: 'off', updated_by: 'browser:browser' },
      { source: 'builtin:http', item: 'http_request', access: 'outward', updated_by: 'browser:browser' },
    ])
    const resolver = new AccessResolver(access)
    expect(await resolver.entry('builtin:http', 'http_request')).toEqual({ enabled: false, access: 'outward' })
    expect(reads.n).toBe(1)
    expect(await resolver.entry('builtin:http', 'other')).toEqual({ enabled: false, access: undefined })
    expect(await new AccessResolver(undefined).entry('builtin:http', 'http_request')).toEqual({ enabled: true, access: undefined })
  })

  // #4757: the on/off items and the two `*` switches.
  it('a package server is on until switched off', async () => {
    const server = 'package:pk/mcp:srv'
    expect(await new AccessResolver(undefined).enabled(server)).toBe(true)
    expect(await new AccessResolver(memoryAccess().access).enabled(server)).toBe(true)
    expect(await new AccessResolver(memoryAccess().access).switchedItem(server, 'search', 'outward')).toBe('outward')
    const { access } = memoryAccess([{ source: server, item: '*', access: 'off', updated_by: BY }])
    expect(await new AccessResolver(access).enabled(server)).toBe(false)
    expect(await new AccessResolver(access).switchedItem(server, 'search', 'outward')).toBe('off')
    expect(await new AccessResolver(access).enabled('package:pk')).toBe(false)
  })

  it('builtin:browser switched off disables every browser tool in registry() and registryOverrides().disabled()', async () => {
    const { access } = memoryAccess([
      { source: B, item: '*', access: 'off', updated_by: BY },
      { source: B, item: 'browser_click', access: 'outward', updated_by: BY },
      { source: S, item: 'get_model', access: 'off', updated_by: BY },
    ])
    const resolver = new AccessResolver(access)
    const registry = await resolver.registry()
    expect(registry.disabled_tools).toEqual([...BROWSER, 'get_model'].sort())
    expect(Object.keys(registry.tool_tiers)).toEqual([])
    expect(await resolver.registryOverrides().disabled()).toEqual(new Set([...BROWSER, 'get_model']))
  })

  it('registryOverrides().of a browser tool answers disabled while its source is off', async () => {
    const { access, rows } = memoryAccess([{ source: B, item: 'browser_click', access: 'outward', updated_by: BY }])
    const overrides = new AccessResolver(access).registryOverrides()
    expect(await overrides.of('browser_click')).toEqual({ tier: 'outward', disabled: false })
    rows.push({ source: B, item: '*', access: 'off', updated_by: BY })
    expect(await overrides.of('browser_click')).toEqual({ disabled: true })
    expect(await overrides.of('get_model')).toEqual({ disabled: false })
  })

  it('switchOn reads an off row as off and anything else as on', async () => {
    const { access } = memoryAccess([
      { source: 'package:pk', item: 'skill:a', access: 'off', updated_by: BY },
      { source: 'package:pk', item: 'skill:b', access: 'on', updated_by: BY },
    ])
    const resolver = new AccessResolver(access)
    expect(await resolver.switchOn('package:pk', 'skill:a')).toBe(false)
    expect(await resolver.switchOn('package:pk', 'skill:b')).toBe(true)
    expect(await resolver.switchOn('package:pk', 'skill:c')).toBe(true)
    expect(await new AccessResolver(undefined).switchOn('package:pk', 'skill:a')).toBe(true)
  })

  it('the stricter built-in access wins', async () => {
    const { access } = memoryAccess([
      { source: 'package:w', item: 'builtin:Bash', access: 'write', updated_by: BY },
      { source: 'package:o', item: 'builtin:Bash', access: 'off', updated_by: BY },
      { source: 'package:r', item: 'builtin:Bash', access: 'read', updated_by: BY },
      { source: 'package:u', item: 'builtin:Bash', access: 'outward', updated_by: BY },
    ])
    const resolver = new AccessResolver(access)
    expect(await resolver.builtin('Bash', ['w', 'o'])).toBe('off')
    expect(await resolver.builtin('Bash', ['r', 'u'])).toBe('outward')
    expect(await resolver.builtin('Bash', ['w', 'unset'])).toBe('outward')
    expect(await resolver.builtin('Bash', ['w'])).toBe('write')
    expect(await resolver.builtin('Bash', [])).toBe('outward')
    expect(await new AccessResolver(undefined).builtin('Bash', ['w'])).toBe('outward')
  })

  it('packageAccess reads every package\'s items in one query', async () => {
    const { access, reads } = memoryAccess([
      { source: 'package:a', item: 'skill:s1', access: 'off', updated_by: BY },
      { source: 'package:a', item: 'skill:s2', access: 'on', updated_by: BY },
      { source: 'package:a', item: 'subagent:sa', access: 'off', updated_by: BY },
      { source: 'package:a', item: 'hook:PreToolUse:0123456789abcdef', access: 'off', updated_by: BY },
      { source: 'package:a', item: 'builtin:Bash', access: 'write', updated_by: BY },
      { source: 'package:a', item: 'builtin:WebFetch', access: 'off', updated_by: BY },
      { source: 'builtin:own-plugin', item: 'skill:print', access: 'off', updated_by: BY },
      { source: 'package:c', item: 'skill:x', access: 'off', updated_by: BY },
    ])
    const got = await new AccessResolver(access).packageAccess(['package:a', 'package:b', 'builtin:own-plugin'])
    expect(reads.n).toBe(1)
    expect([...got.keys()]).toEqual(['package:a', 'package:b', 'builtin:own-plugin'])
    expect(got.get('package:a')).toEqual({
      off: { skills: new Set(['s1']), subagents: new Set(['sa']), hooks: new Set(['PreToolUse:0123456789abcdef']), builtins: new Set(['WebFetch']) },
      builtinTiers: new Map([['Bash', 'write']]),
    })
    expect(got.get('package:b')).toEqual({ off: { skills: new Set(), subagents: new Set(), hooks: new Set(), builtins: new Set() }, builtinTiers: new Map() })
    expect(got.get('builtin:own-plugin')!.off.skills).toEqual(new Set(['print']))
  })

  it('a read that fails rejects', async () => {
    const reader: AccessReader = { rows: () => Promise.reject(new Error('db down')) }
    const resolver = new AccessResolver(reader)
    await expect(resolver.registry()).rejects.toThrow('db down')
    await expect(resolver.registryOverrides().of('get_model')).rejects.toThrow('db down')
    await expect(resolver.item('remote:hs', 'search', 'outward')).rejects.toThrow('db down')
    await expect(resolver.entry('builtin:http', 'http_request')).rejects.toThrow('db down')
  })
})

describe('access items (#4757)', () => {
  it('names each item kind by its prefix, anything else a tool', () => {
    expect([skillItem('a'), subagentItem('b'), builtinItem('Bash'), hookItem('PreToolUse', '0123456789abcdef')]).toEqual([
      'skill:a',
      'subagent:b',
      'builtin:Bash',
      'hook:PreToolUse:0123456789abcdef',
    ])
    expect(['skill:a', 'subagent:b', 'builtin:Bash', 'hook:Stop:0123456789abcdef', 'get_model', 'skillful'].map(itemKindOf)).toEqual([
      'skill',
      'subagent',
      'builtin',
      'hook',
      'tool',
      'tool',
    ])
  })

  it('strictest orders off > outward > write > read', () => {
    expect(BUILTIN_STRICTNESS).toEqual(['read', 'write', 'outward', 'off'])
    expect(strictest(['read', 'off', 'write'])).toBe('off')
    expect(strictest(['read', 'write'])).toBe('write')
    expect(strictest([])).toBeUndefined()
  })

  it('pluginOfName splits at the first colon', () => {
    expect(pluginOfName('scadbuddy:print')).toEqual({ plugin: 'scadbuddy', name: 'print' })
    expect(pluginOfName('a:b:c')).toEqual({ plugin: 'a', name: 'b:c' })
    expect(pluginOfName('plain')).toBeUndefined()
    expect(pluginOfName(':x')).toBeUndefined()
    expect(pluginOfName('x:')).toBeUndefined()
  })
})
