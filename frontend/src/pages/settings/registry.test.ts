import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { TOOLS } from '../../agent/catalog'
import {
  AREAS,
  foldIdsOf,
  isAreaId,
  LEGACY_ANCHORS,
  loadSections,
  resolveLocation,
  SECTION_REGISTRY,
  settingsTarget,
} from './registry'

const Component = () => null
const mod = (id: string, area: string, order: number) => ({ id, area, title: id, order, Component })

const sections = loadSections({
  './sections/packages.tsx': mod('packages', 'tools', 20),
  './sections/model.tsx': mod('model', 'assistant', 20),
  './sections/fonts.tsx': mod('fonts', 'library', 30),
  './sections/connection.tsx': mod('connection', 'general', 10),
})

describe('registry', () => {
  it('lists the areas in order', () => {
    expect(AREAS.map((a) => a.title)).toEqual([
      'General',
      'Printing',
      'Library',
      'Assistant',
      'Tools & plugins',
      'MCP access',
      'Audits',
      'System',
    ])
    expect(AREAS.filter((a) => a.servedByAgent).map((a) => a.id)).toEqual([
      'assistant',
      'tools',
      'mcp',
      'audits',
    ])
    expect(isAreaId('tools')).toBe(true)
    expect(isAreaId('bogus')).toBe(false)
    expect(isAreaId(undefined)).toBe(false)
  })

  it('names the file that forgets an export', () => {
    expect(() =>
      loadSections({ './sections/x.tsx': { id: 'x', area: 'general', title: 'X', order: 1 } }),
    ).toThrow('settings section ./sections/x.tsx must export id, area, title, order and Component')
  })

  it('refuses an area it does not know', () => {
    expect(() => loadSections({ './sections/x.tsx': mod('x', 'nowhere', 1) })).toThrow(
      'settings section ./sections/x.tsx must export id, area, title, order and Component',
    )
  })

  it('refuses two sections with one id', () => {
    expect(() =>
      loadSections({
        './sections/a.tsx': mod('fonts', 'library', 1),
        './sections/b.tsx': mod('fonts', 'tools', 2),
      }),
    ).toThrow('duplicate settings section id "fonts"')
  })

  it('orders by area, then by order', () => {
    expect(sections.map((s) => s.id)).toEqual(['connection', 'fonts', 'model', 'packages'])
  })

  it('opens the area that holds a section', () => {
    expect(settingsTarget('#fonts', sections)).toEqual({ area: 'library', hash: '#fonts' })
  })

  it('maps every old anchor to its new section', () => {
    expect(settingsTarget('#assistant-plugins', sections)).toEqual({ area: 'tools', hash: '#packages' })
    expect(settingsTarget('#assistant-model', sections)).toEqual({ area: 'assistant', hash: '#model' })
    expect(settingsTarget('#assistant', sections)).toEqual({
      area: 'assistant',
      hash: '#assistant-credentials',
    })
    expect(settingsTarget('#assistant-audit', sections)).toEqual({ area: 'audits', hash: '#audit-log' })
  })

  it('knows nothing of an id no area holds, or a malformed escape (#2113)', () => {
    expect(settingsTarget('#nope', sections)).toBeNull()
    expect(settingsTarget('#%E0%A4%A', sections)).toBeNull()
  })

  it('resolves the address', () => {
    expect(resolveLocation('/settings', '', sections)).toEqual({ area: 'general', hash: '' })
    expect(resolveLocation('/settings', '#fonts', sections)).toEqual({ area: 'library', hash: '#fonts' })
    expect(resolveLocation('/settings/library', '#connection', sections)).toEqual({
      area: 'general',
      hash: '#connection',
    })
    expect(resolveLocation('/settings/bogus', '', sections)).toEqual({ area: 'general', hash: '' })
    expect(resolveLocation('/settings/tools', '#nope', sections)).toEqual({ area: 'tools', hash: '#nope' })
    expect(resolveLocation('/settings/', '', sections)).toEqual({ area: 'general', hash: '' })
  })

  it('folds every section it holds', () => {
    expect(foldIdsOf(sections)).toEqual(['connection', 'fonts', 'model', 'packages'])
  })

  it('holds the spec §3.2 table, with Printers and Orders in Printing', () => {
    expect(AREAS.map((a) => [a.id, SECTION_REGISTRY.filter((s) => s.area === a.id).map((s) => s.id)])).toEqual([
      ['general', ['connection']],
      ['printing', ['printing', 'printers', 'inventory', 'orders', 'remembered', 'hotend-usage']],
      ['library', ['projects', 'uploads', 'fonts', 'libraries', 'rendering', 'preview']],
      [
        'assistant',
        [
          'assistant-credentials',
          'model',
          'session-limits',
          'session-runtime',
          'render-limits',
          'images',
          'workspace',
          'default-session-mode',
          'durable-subagents',
          'memory',
        ],
      ],
      ['tools', ['builtin', 'packages', 'remote']],
      ['mcp', ['mcp-auth-mode', 'mcp-access-tokens', 'mcp-oidc', 'mcp-connect']],
      ['audits', ['audit-log']],
      ['system', ['diagnostics', 'administration', 'about']],
    ])
  })

  it('sends every old anchor to a section that exists in its area', () => {
    for (const [anchor, { area, section }] of Object.entries(LEGACY_ANCHORS)) {
      expect(SECTION_REGISTRY.find((s) => s.id === section)?.area, anchor).toBe(area)
      expect(settingsTarget(`#${anchor}`)).toEqual({ area, hash: `#${section}` })
    }
  })
})

describe('the navigate tool', () => {
  it("names every area in the navigate tool's description", () => {
    for (const { id } of AREAS) expect(TOOLS.navigate.description).toContain(`"/settings/${id}"`)
  })

  it("names every area in the agent's navigate description (agent/src/tools/browser.ts)", () => {
    const source = readFileSync(resolve(process.cwd(), '../agent/src/tools/browser.ts'), 'utf8')
    for (const { id } of AREAS) expect(source).toContain(`"/settings/${id}"`)
  })
})
