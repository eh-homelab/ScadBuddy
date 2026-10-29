import { describe, expect, it } from 'vitest'
import type { ItemView, SourceView } from '../../api/capabilities'
import { choicesOf, lowers, matches, shownAccess, summarize } from './access'

function item(over: Partial<ItemView> = {}): ItemView {
  return {
    id: 'list_models',
    kind: 'tool',
    name: 'list_models',
    harness_name: 'mcp__scadbuddy__list_models',
    label: 'List models',
    description: 'Lists every model in the library.',
    default: 'read',
    default_note: null,
    suggested: null,
    access: 'read',
    // The item's own access follows `access` unless a test sets it.
    own_access: over.access === undefined ? 'read' : over.access,
    overridden: false,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
    ...over,
  }
}

function source(items: ItemView[], over: Partial<SourceView> = {}): SourceView {
  return {
    id: 'builtin:scadbuddy',
    kind: 'builtin',
    title: 'ScadBuddy tools',
    enabled: true,
    switchable: false,
    options_route: null,
    listed_at: null,
    truncated: false,
    items,
    children: [],
    ...over,
  }
}

describe('access rules', () => {
  it('marks the default and words outward', () => {
    expect(choicesOf(item({ default: 'outward', access: 'outward' }))).toEqual([
      { value: 'off', label: 'off' },
      { value: 'read', label: 'read' },
      { value: 'write', label: 'write' },
      { value: 'outward', label: 'outward (asks first) (default)' },
    ])
    expect(choicesOf(item({ kind: 'skill', default: 'on', access: 'on' }))).toEqual([
      { value: 'off', label: 'off' },
      { value: 'on', label: 'on (default)' },
    ])
  })

  it('offers By call first for an item whose default depends on the call', () => {
    const http = item({ default: null, access: null, default_note: 'read for GET and HEAD' })
    expect(choicesOf(http)).toEqual([
      { value: 'default', label: 'By call (default)' },
      { value: 'off', label: 'off' },
      { value: 'read', label: 'read' },
      { value: 'write', label: 'write' },
      { value: 'outward', label: 'outward (asks first)' },
    ])
    expect(shownAccess(http)).toBe('default')
    expect(shownAccess({ ...http, access: 'write', own_access: 'write', overridden: true })).toBe('write')
  })

  it('offers only the allowed accesses of a renamed tool', () => {
    const renamed = item({ default: 'outward', access: 'outward', allowed: ['off', 'outward'] })
    expect(choicesOf(renamed)).toEqual([
      { value: 'off', label: 'off' },
      { value: 'outward', label: 'outward (asks first) (default)' },
    ])
  })

  it('lowering is below the default, raising is not', () => {
    const outward = item({ default: 'outward', access: 'outward' })
    expect(lowers(outward, 'write')).toBe(true)
    expect(lowers(outward, 'off')).toBe(true)
    expect(lowers(outward, 'outward')).toBe(false)
    const write = item({ default: 'write', access: 'write' })
    expect(lowers(write, 'read')).toBe(true)
    const read = item({ default: 'read', access: 'read' })
    expect(lowers(read, 'write')).toBe(false)
    const byCall = item({ default: null, access: null })
    expect(lowers(byCall, 'read')).toBe(true)
  })

  it('choosing what is shown is no lowering', () => {
    const off = item({ default: 'outward', access: 'off', overridden: true })
    expect(shownAccess(off)).toBe('off')
    expect(lowers(off, 'off')).toBe(false)
    const fixed = item({ default: 'outward', access: 'off', fixed: { access: 'off', reason: 'Shares its name' } })
    expect(shownAccess(fixed)).toBe('off')
    expect(lowers(fixed, 'off')).toBe(false)
    // A stored row the rule ignores: the default is shown.
    expect(shownAccess(item({ default: 'write', access: 'read', overridden: false }))).toBe('write')
  })

  it("shows the item's own access while its source is off", () => {
    const off = item({ default: 'outward', access: 'off', own_access: 'write', overridden: true, reason: 'Off while "hs" is switched off' })
    expect(shownAccess(off)).toBe('write')
    expect(lowers(off, 'write')).toBe(false)
    expect(lowers(off, 'off')).toBe(true)
    // No row of its own: the default, whatever the switch makes it.
    expect(shownAccess(item({ default: 'outward', access: 'off', own_access: 'outward', overridden: false }))).toBe('outward')
  })

  it('summarizes changed items', () => {
    const many = Array.from({ length: 115 }, (_, i) => item({ id: `t${i}`, overridden: i < 3 }))
    expect(summarize(source(many))).toBe('115 tools, 3 changed')
    expect(summarize(source([item()]))).toBe('1 tool')
    expect(summarize(source([item(), item({ id: 'b', overridden: true })]))).toBe('2 tools, 1 changed')
    expect(summarize(source([], { id: 'remote:hindsight', kind: 'remote' }))).toBe('No tools listed yet')
    expect(summarize(source([], { id: 'builtin:own-plugin' }))).toBe('Nothing listed')
  })

  it('filters by label, name and description', () => {
    const it = item({ label: 'Send to printer', name: 'send_to_printer', description: 'Starts a PRINT on the machine.' })
    expect(matches(it, 'SEND')).toBe(true)
    expect(matches(it, 'to_printer')).toBe(true)
    expect(matches(it, 'print on')).toBe(true)
    expect(matches(it, '')).toBe(true)
    expect(matches(it, 'camera')).toBe(false)
    expect(matches(item({ description: null }), 'lists')).toBe(false)
  })
})
