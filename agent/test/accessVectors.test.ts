import { readFileSync, writeFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { type AccessRow, MIGRATED_BY, registrySource, type StoredOverrides, TOOL_ACCESS } from '../src/access/model.js'
import { type CallPlugin, type ItemKind, itemKindIn, offCallItem } from '../src/access/items.js'
import { registryOverrides, sourceEnabled } from '../src/access/resolve.js'
import { AccessResolver } from '../src/access/resolver.js'
import type { AccessReader } from '../src/access/store.js'
import type { RiskTier } from '../src/harness/permissions.js'

// The tool-access rules (#2171, spec 2026-10-18 §4.1–§4.3). The vectors are the one source of
// truth for them: agent-durable's tests open this file and apply the same rules in Python.
// Regenerate: UPDATE_ACCESS_VECTORS=1 pnpm vitest run test/accessVectors.test.ts
const FILE = new URL('./fixtures/access-vectors.json', import.meta.url)

type VectorTool = { name: string; risk: RiskTier | '?'; hitl: 'approval' | 'answer' | null }
type RegistryVector = { name: string; durable_only?: true; tools: VectorTool[]; rows: AccessRow[]; expect: StoredOverrides }
type SourceVector = { name: string; source: string; rows: AccessRow[]; expect: boolean }
type ItemKindVector = { source: string; item: string; expect: ItemKind }
/** One Skill, Agent or Task call (#4757): the item it names that is off, or null. */
type CallVector = { name: string; tool: string; input: unknown; plugins: CallPlugin[]; rows: AccessRow[]; expect: string | null }
type Vectors = {
  tool_access: string[]
  item_kinds: ItemKindVector[]
  migrated_by: string
  registry_source: Record<string, string>
  registry: RegistryVector[]
  sources: SourceVector[]
  calls: CallVector[]
}

const S = 'builtin:scadbuddy'
const B = 'builtin:browser'
const BY = 'browser:browser'

const GET_MODEL: VectorTool = { name: 'get_model', risk: 'read', hitl: null }
const SEND: VectorTool = { name: 'send_to_bambuddy', risk: 'outward', hitl: 'approval' }
const CLICK: VectorTool = { name: 'browser_click', risk: 'write', hitl: null }
const TOOLS = [GET_MODEL, SEND, CLICK]

const row = (source: string, item: string, access: string, updated_by = BY): AccessRow => ({ source, item, access, updated_by })
const none: StoredOverrides = { tool_tiers: {}, disabled_tools: [] }

const PA = 'package:pa'
const PB = 'package:pb'
const OWN = 'builtin:own-plugin'
/** Two packages and the own plugin; `pa`'s `agents/review/deep.md` declares `name: deep-review`, two skills declare `name: tidy`. */
const PLUGINS: CallPlugin[] = [
  { name: 'pa', source: PA, aliases: { 'subagent:deep-review': ['subagent:review:deep'], 'skill:tidy': ['skill:sweep', 'skill:dust'] } },
  { name: 'pb', source: PB, aliases: {} },
  { name: 'scadbuddy', source: OWN, aliases: {} },
]

function build(): Vectors {
  return {
    tool_access: [...TOOL_ACCESS],
    // #4757 review I1: only a package's own source and the own plugin hold named items.
    item_kinds: [
      { source: 'package:pk', item: 'skill:a', expect: 'skill' },
      { source: 'package:pk', item: 'subagent:a', expect: 'subagent' },
      { source: 'package:pk', item: 'hook:Stop:0123456789abcdef', expect: 'hook' },
      { source: 'package:pk', item: 'builtin:Bash', expect: 'builtin' },
      { source: 'package:pk', item: 'search', expect: 'tool' },
      { source: 'builtin:own-plugin', item: 'skill:print', expect: 'skill' },
      { source: 'remote:hs', item: 'skill:x', expect: 'tool' },
      { source: 'package:pk/mcp:srv', item: 'skill:x', expect: 'tool' },
      { source: 'builtin:scadbuddy', item: 'builtin:x', expect: 'tool' },
      { source: 'builtin:http', item: 'hook:Stop:0123456789abcdef', expect: 'tool' },
    ],
    migrated_by: MIGRATED_BY,
    registry_source: {
      get_model: S,
      send_to_bambuddy: S,
      get_printer_camera: S,
      order_accept: S,
      browser_click: B,
      browser_navigate: B,
    },
    registry: [
      { name: 'no rows: every tool at its own tier', tools: TOOLS, rows: [], expect: none },
      { name: 'a raise of a read tool', tools: TOOLS, rows: [row(S, 'get_model', 'outward')], expect: { tool_tiers: { get_model: 'outward' }, disabled_tools: [] } },
      { name: 'a lowering of an outward tool', tools: TOOLS, rows: [row(S, 'send_to_bambuddy', 'write')], expect: { tool_tiers: { send_to_bambuddy: 'write' }, disabled_tools: [] } },
      {
        name: 'off disables',
        tools: TOOLS,
        rows: [row(S, 'send_to_bambuddy', 'off'), row(B, 'browser_click', 'off'), row(S, 'get_model', 'off')],
        expect: { tool_tiers: {}, disabled_tools: ['browser_click', 'get_model', 'send_to_bambuddy'] },
      },
      { name: "a tool's own tier is no override", tools: TOOLS, rows: [row(S, 'get_model', 'read'), row(B, 'browser_click', 'write')], expect: none },
      { name: 'a row naming no tool is ignored', tools: TOOLS, rows: [row(S, 'no_such_tool', 'off'), row(S, '*', 'off')], expect: none },
      { name: 'an unknown access value is ignored', tools: TOOLS, rows: [row(S, 'get_model', 'none'), row(S, 'send_to_bambuddy', 'on')], expect: none },
      { name: 'a migrated row that lowers is ignored', tools: TOOLS, rows: [row(S, 'send_to_bambuddy', 'write', MIGRATED_BY)], expect: none },
      {
        name: 'a migrated row that raises counts',
        tools: TOOLS,
        rows: [row(S, 'get_model', 'write', MIGRATED_BY)],
        expect: { tool_tiers: { get_model: 'write' }, disabled_tools: [] },
      },
      { name: 'a migrated off counts', tools: TOOLS, rows: [row(S, 'send_to_bambuddy', 'off', MIGRATED_BY)], expect: { tool_tiers: {}, disabled_tools: ['send_to_bambuddy'] } },
      {
        name: 'browser tools come from builtin:browser',
        tools: TOOLS,
        rows: [row(S, 'browser_click', 'off'), row(B, 'browser_click', 'outward'), row(B, 'get_model', 'off')],
        expect: { tool_tiers: { browser_click: 'outward' }, disabled_tools: [] },
      },
      { name: 'a row of another source is ignored', tools: TOOLS, rows: [row('remote:hs', 'get_model', 'off'), row('builtin:http', 'send_to_bambuddy', 'read')], expect: none },
      {
        name: 'an answer tool is in no source',
        tools: [...TOOLS, { name: 'ask_user', risk: 'read', hitl: 'answer' }],
        rows: [row(S, 'ask_user', 'off')],
        expect: none,
      },
      {
        // An own key in both languages: a JS object literal would take it as its prototype.
        name: 'a tool named __proto__ is an own key',
        tools: [{ name: '__proto__', risk: 'read', hitl: null }],
        rows: [row(S, '__proto__', 'write')],
        expect: { tool_tiers: JSON.parse('{"__proto__":"write"}') as Record<string, RiskTier>, disabled_tools: [] },
      },
      {
        name: 'durable_only: a migrated raise of a tool whose tier is no tier counts',
        durable_only: true,
        tools: [{ name: 't', risk: '?', hitl: null }],
        rows: [row(S, 't', 'outward', MIGRATED_BY)],
        expect: { tool_tiers: { t: 'outward' }, disabled_tools: [] },
      },
      {
        // #4757: the browser's `*` switch, folded into every gate's overrides.
        name: 'browser switched off disables the browser tools',
        tools: [...TOOLS, { name: 'browser_navigate', risk: 'read', hitl: null }],
        rows: [row(B, '*', 'off'), row(B, 'browser_click', 'outward'), row(S, 'get_model', 'write')],
        expect: { tool_tiers: { get_model: 'write' }, disabled_tools: ['browser_click', 'browser_navigate'] },
      },
      {
        name: 'browser switched on keeps their rows',
        tools: TOOLS,
        rows: [row(B, '*', 'on'), row(B, 'browser_click', 'outward')],
        expect: { tool_tiers: { browser_click: 'outward' }, disabled_tools: [] },
      },
    ],
    sources: [
      { name: 'http is on by default', source: 'builtin:http', rows: [], expect: true },
      { name: 'http off', source: 'builtin:http', rows: [row('builtin:http', '*', 'off')], expect: false },
      { name: 'the own plugin off', source: 'builtin:own-plugin', rows: [row('builtin:own-plugin', '*', 'off')], expect: false },
      { name: 'a remote plugin is off by default', source: 'remote:hs', rows: [], expect: false },
      { name: 'a remote plugin on', source: 'remote:hs', rows: [row('remote:hs', '*', 'on')], expect: true },
      { name: "a '*' row holding a tier is ignored", source: 'remote:hs', rows: [row('remote:hs', '*', 'outward')], expect: false },
      { name: 'a package server is on by default', source: 'package:pk/mcp:srv', rows: [], expect: true },
      { name: 'a package server off', source: 'package:pk/mcp:srv', rows: [row('package:pk/mcp:srv', '*', 'off')], expect: false },
      { name: 'a package is off by default', source: 'package:pk', rows: [], expect: false },
      { name: 'a source with a newline is no package server', source: 'package:pk\n/mcp:srv', rows: [], expect: false },
    ],
    calls: [
      { name: 'a qualified skill off', tool: 'Skill', input: { skill: 'pa:jot' }, plugins: PLUGINS, rows: [row(PA, 'skill:jot', 'off')], expect: 'skill:jot' },
      { name: 'a qualified skill of another plugin', tool: 'Skill', input: { skill: 'pb:jot' }, plugins: PLUGINS, rows: [row(PA, 'skill:jot', 'off')], expect: null },
      { name: 'an unqualified skill off in one plugin', tool: 'Skill', input: { skill: 'jot' }, plugins: PLUGINS, rows: [row(PB, 'skill:jot', 'off')], expect: 'skill:jot' },
      {
        name: 'an unqualified subagent off in one plugin',
        tool: 'Agent',
        input: { subagent_type: 'reviewer', prompt: 'p' },
        plugins: PLUGINS,
        rows: [row(PB, 'subagent:reviewer', 'off')],
        expect: 'subagent:reviewer',
      },
      {
        name: 'a subagent by its frontmatter name',
        tool: 'Agent',
        input: { subagent_type: 'pa:deep-review' },
        plugins: PLUGINS,
        rows: [row(PA, 'subagent:review:deep', 'off')],
        expect: 'subagent:review:deep',
      },
      {
        name: 'a nested subagent named without its plugin',
        tool: 'Agent',
        input: { subagent_type: 'review:deep' },
        plugins: PLUGINS,
        rows: [row(PA, 'subagent:review:deep', 'off')],
        expect: 'subagent:review:deep',
      },
      { name: 'Task as Agent', tool: 'Task', input: { subagent_type: 'pa:reviewer' }, plugins: PLUGINS, rows: [row(PA, 'subagent:reviewer', 'off')], expect: 'subagent:reviewer' },
      { name: 'an alias with several targets, one off', tool: 'Skill', input: { skill: 'pa:tidy' }, plugins: PLUGINS, rows: [row(PA, 'skill:dust', 'off')], expect: 'skill:dust' },
      { name: "an alias is its own plugin's only", tool: 'Skill', input: { skill: 'pb:tidy' }, plugins: PLUGINS, rows: [row(PB, 'skill:dust', 'off')], expect: null },
      { name: "the own plugin's skill off", tool: 'Skill', input: { skill: 'scadbuddy:print' }, plugins: PLUGINS, rows: [row(OWN, 'skill:print', 'off')], expect: 'skill:print' },
      { name: 'an on row refuses nothing', tool: 'Skill', input: { skill: 'pa:jot' }, plugins: PLUGINS, rows: [row(PA, 'skill:jot', 'on')], expect: null },
      { name: 'a skill row does not refuse a subagent', tool: 'Agent', input: { subagent_type: 'pa:jot' }, plugins: PLUGINS, rows: [row(PA, 'skill:jot', 'off')], expect: null },
      { name: 'a row of an unloaded source refuses nothing', tool: 'Skill', input: { skill: 'pc:jot' }, plugins: PLUGINS, rows: [row('package:pc', 'skill:jot', 'off')], expect: null },
      { name: 'a tool that is not Skill, Agent or Task', tool: 'Bash', input: { skill: 'pa:jot', subagent_type: 'pa:jot' }, plugins: PLUGINS, rows: [row(PA, 'skill:jot', 'off'), row(PA, 'subagent:jot', 'off')], expect: null },
      { name: 'an input without the field', tool: 'Skill', input: { subagent_type: 'pa:jot' }, plugins: PLUGINS, rows: [row(PA, 'skill:jot', 'off')], expect: null },
      { name: 'an input that is not an object', tool: 'Agent', input: 'pa:reviewer', plugins: PLUGINS, rows: [row(PA, 'subagent:reviewer', 'off')], expect: null },
    ],
  }
}

describe('access vectors', () => {
  const built = build()

  it('match the committed file', () => {
    if (process.env.UPDATE_ACCESS_VECTORS === '1') writeFileSync(FILE, `${JSON.stringify(built, null, 2)}\n`)
    expect(JSON.parse(readFileSync(FILE, 'utf8'))).toEqual(built)
  })

  it.each(built.registry.filter((v) => !v.durable_only))('registry: $name', (v) => {
    const tools = v.tools.filter((t) => t.hitl !== 'answer') as { name: string; risk: RiskTier }[]
    expect(registryOverrides(tools, v.rows)).toEqual(v.expect)
  })

  // Review of #4838: a call's tool is resolved from its one row, not the whole registry.
  it.each(built.registry.filter((v) => !v.durable_only))('one tool per call, as the whole registry: $name', async (v) => {
    const tools = v.tools.filter((t) => t.hitl !== 'answer') as { name: string; risk: RiskTier }[]
    const reads = { rows: 0, row: 0 }
    const reader: AccessReader = {
      rows: async (sources) => {
        reads.rows += 1
        return v.rows.filter((r) => sources.includes(r.source))
      },
      row: async (source, item) => {
        reads.row += 1
        return v.rows.find((r) => r.source === source && r.item === item)
      },
    }
    const overrides = new AccessResolver(reader, tools).registryOverrides()
    for (const tool of tools) {
      const tier = v.expect.tool_tiers[tool.name]
      expect(await overrides.of(tool.name), tool.name).toEqual({ ...(tier === undefined ? {} : { tier }), disabled: v.expect.disabled_tools.includes(tool.name) })
    }
    // A browser tool's call reads its source's rows once, for the `*` switch too (#4757).
    const browser = tools.filter((t) => registrySource(t.name) === B).length
    expect(reads).toEqual({ rows: browser, row: tools.length - browser })
  })

  it.each(built.item_kinds)('item kind: $item in $source', (v) => expect(itemKindIn(v.source, v.item)).toBe(v.expect))

  it.each(built.sources)('source: $name', (v) => expect(sourceEnabled(v.source, v.rows)).toBe(v.expect))

  it.each(built.calls)('call: $name', (v) => expect(offCallItem(v.tool, v.input, v.plugins, v.rows) ?? null).toBe(v.expect))

  it("names each tool's registry source", () => {
    for (const [tool, source] of Object.entries(built.registry_source)) expect(registrySource(tool)).toBe(source)
  })
})
