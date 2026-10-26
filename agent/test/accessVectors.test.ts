import { readFileSync, writeFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { type AccessRow, MIGRATED_BY, registrySource, type StoredOverrides, TOOL_ACCESS } from '../src/access/model.js'
import { registryOverrides, sourceEnabled } from '../src/access/resolve.js'
import type { RiskTier } from '../src/harness/permissions.js'

// The tool-access rules (#2171, spec 2026-10-18 §4.1–§4.3). The vectors are the one source of
// truth for them: agent-durable's tests open this file and apply the same rules in Python.
// Regenerate: UPDATE_ACCESS_VECTORS=1 pnpm vitest run test/accessVectors.test.ts
const FILE = new URL('./fixtures/access-vectors.json', import.meta.url)

type VectorTool = { name: string; risk: RiskTier | '?'; hitl: 'approval' | 'answer' | null }
type RegistryVector = { name: string; durable_only?: true; tools: VectorTool[]; rows: AccessRow[]; expect: StoredOverrides }
type SourceVector = { name: string; source: string; rows: AccessRow[]; expect: boolean }
type Vectors = {
  tool_access: string[]
  migrated_by: string
  registry_source: Record<string, string>
  registry: RegistryVector[]
  sources: SourceVector[]
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

function build(): Vectors {
  return {
    tool_access: [...TOOL_ACCESS],
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
        name: 'durable_only: a migrated raise of a tool whose tier is no tier counts',
        durable_only: true,
        tools: [{ name: 't', risk: '?', hitl: null }],
        rows: [row(S, 't', 'outward', MIGRATED_BY)],
        expect: { tool_tiers: { t: 'outward' }, disabled_tools: [] },
      },
    ],
    sources: [
      { name: 'http is on by default', source: 'builtin:http', rows: [], expect: true },
      { name: 'http off', source: 'builtin:http', rows: [row('builtin:http', '*', 'off')], expect: false },
      { name: 'the own plugin off', source: 'builtin:own-plugin', rows: [row('builtin:own-plugin', '*', 'off')], expect: false },
      { name: 'a remote plugin is off by default', source: 'remote:hs', rows: [], expect: false },
      { name: 'a remote plugin on', source: 'remote:hs', rows: [row('remote:hs', '*', 'on')], expect: true },
      { name: "a '*' row holding a tier is ignored", source: 'remote:hs', rows: [row('remote:hs', '*', 'outward')], expect: false },
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

  it.each(built.sources)('source: $name', (v) => expect(sourceEnabled(v.source, v.rows)).toBe(v.expect))

  it("names each tool's registry source", () => {
    for (const [tool, source] of Object.entries(built.registry_source)) expect(registrySource(tool)).toBe(source)
  })
})
