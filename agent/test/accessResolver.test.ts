import { describe, expect, it } from 'vitest'
import { MIGRATED_BY } from '../src/access/model.js'
import { AccessResolver } from '../src/access/resolver.js'
import type { AccessReader } from '../src/access/store.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { memoryAccess, rowsFromOverrides } from './support/memoryAccess.js'

const S = 'builtin:scadbuddy'

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

  it('a read that fails rejects', async () => {
    const reader: AccessReader = { rows: () => Promise.reject(new Error('db down')) }
    const resolver = new AccessResolver(reader)
    await expect(resolver.registry()).rejects.toThrow('db down')
    await expect(resolver.registryOverrides().of('get_model')).rejects.toThrow('db down')
    await expect(resolver.item('remote:hs', 'search', 'outward')).rejects.toThrow('db down')
    await expect(resolver.entry('builtin:http', 'http_request')).rejects.toThrow('db down')
  })
})
