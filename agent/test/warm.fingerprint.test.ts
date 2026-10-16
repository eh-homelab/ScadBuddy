import { describe, expect, it } from 'vitest'
import { costStateUsd, fingerprint, restoredCostUsd } from '../src/sessions/warm.js'
import { type FingerprintParts, TurnRunner } from '../src/sessions/turnRunner.js'

const plugin = { id: 'p', url: 'https://x', headerVersion: '1', toolTiers: ['a=read'], disabledTools: ['b'] }
const parts: FingerprintParts = {
  credentialId: 'c1',
  credentialUpdatedAt: '1',
  model: null,
  ownPlugin: true,
  plugins: [plugin],
  packageHashes: ['h2', 'h1'],
  allowedPackageHashes: ['a2', 'a1'],
  tiers: null,
  owner: 'user:alice',
  systemAppend: 'a',
  disabledTools: [],
  raisedTiers: [],
  httpRequest: true,
  builtinTools: [],
  recallMaxQueryChars: null,
  transientRetries: null,
  servedBuiltins: [],
  backgroundTasks: false,
  effort: null,
  agentType: null,
}

describe('fingerprint', () => {
  it('is a sha256 hex', () => {
    expect(fingerprint(parts)).toMatch(/^[0-9a-f]{64}$/)
  })
  it('ignores array and key order', () => {
    expect(fingerprint({ ...parts, packageHashes: ['h1', 'h2'], allowedPackageHashes: ['a1', 'a2'] })).toBe(fingerprint(parts))
    const two = { ...parts, plugins: [plugin, { ...plugin, id: 'q' }] }
    expect(fingerprint({ ...two, plugins: [...two.plugins].reverse() })).toBe(fingerprint(two))
    const reordered = Object.fromEntries(Object.entries(parts).reverse()) as FingerprintParts
    expect(fingerprint(reordered)).toBe(fingerprint(parts))
  })
  it('changes with any field', () => {
    const changed: Record<keyof FingerprintParts, unknown> = {
      credentialId: 'c2',
      credentialUpdatedAt: 'x',
      model: 'm',
      ownPlugin: false,
      plugins: [],
      packageHashes: ['h1'],
      allowedPackageHashes: ['a1'],
      tiers: ['read'],
      owner: 'user:bob',
      systemAppend: 'b',
      disabledTools: ['Bash'],
      raisedTiers: ['Bash=write'],
      httpRequest: false,
      builtinTools: ['Bash'],
      recallMaxQueryChars: 12,
      transientRetries: 2,
      servedBuiltins: ['WebFetch'],
      backgroundTasks: true,
      effort: 'low',
      agentType: 'model-author',
    }
    expect(Object.keys(changed).sort()).toEqual(Object.keys(parts).sort())
    for (const [k, v] of Object.entries(changed)) expect(fingerprint({ ...parts, [k]: v }), k).not.toBe(fingerprint(parts))
  })
  it('changes with a field inside a plugin', () => {
    for (const k of Object.keys(plugin) as (keyof typeof plugin)[]) {
      const v = Array.isArray(plugin[k]) ? ['z=write'] : 'z'
      expect(fingerprint({ ...parts, plugins: [{ ...plugin, [k]: v }] }), k).not.toBe(fingerprint(parts))
    }
  })
})

describe('TurnRunner.FINGERPRINTED_BY', () => {
  it('covers every option fixed at spawn', () => {
    for (const key of TurnRunner.FIXED_AT_SPAWN) expect(TurnRunner.FINGERPRINTED_BY[key], key).toBeDefined()
  })
  it('names only real parts', () => {
    const names = new Set([...Object.keys(parts), 'slot', 'spawn'])
    for (const [key, by] of Object.entries(TurnRunner.FINGERPRINTED_BY)) expect(names.has(by), key).toBe(true)
  })
})

describe('costStateUsd', () => {
  it('reads the total a cost-state entry carries', () => {
    expect(costStateUsd(JSON.stringify({ type: 'cost-state', totalCostUSD: 0.25 }), () => {})).toBe(0.25)
    expect(costStateUsd(null, () => {})).toBe(0)
  })
  it('takes a malformed row as no baseline, and says so', () => {
    const logged: string[] = []
    expect(costStateUsd('{"type":"cost-state", nope', (m) => logged.push(m))).toBe(0)
    expect(costStateUsd(JSON.stringify({ type: 'cost-state', totalCostUSD: 'x' }), (m) => logged.push(m))).toBe(0)
    expect(logged).toHaveLength(2)
  })
})

describe('restoredCostUsd', () => {
  const id = '11111111-1111-4111-8111-111111111111'
  const state = (totalCostUSD: number, change: Record<string, unknown> = {}) => ({
    type: 'cost-state',
    sessionId: id,
    totalCostUSD,
    totalAPIDuration: 1,
    totalAPIDurationWithoutRetries: 1,
    totalToolDuration: 0,
    totalLinesAdded: 0,
    totalLinesRemoved: 0,
    totalDuration: 1,
    startTime: 1,
    modelUsage: {
      'claude-sonnet-4-5': { inputTokens: 10, outputTokens: 5, cacheReadInputTokens: 0, cacheCreationInputTokens: 0, webSearchRequests: 0, costUSD: totalCostUSD },
    },
    ...change,
  })
  it('reads the last cost-state of the session that Claude Code would restore', () => {
    expect(restoredCostUsd([{ type: 'user', uuid: 'u' }, state(0.1), state(0.2)], id, () => {})).toBe(0.2)
    expect(restoredCostUsd([state(0.1, { laterField: 1 })], id, () => {})).toBe(0.1)
    expect(restoredCostUsd([], id, () => {})).toBe(0)
  })
  it('passes over the ones it would refuse, and says so', () => {
    const logged: string[] = []
    const log = (m: string) => logged.push(m)
    const { totalAPIDurationWithoutRetries: _, ...older } = state(0.3)
    expect(restoredCostUsd([state(0.1), older], id, log)).toBe(0.1)
    expect(restoredCostUsd([state(0.3, { sessionId: '22222222-2222-4222-8222-222222222222' })], id, log)).toBe(0)
    expect(restoredCostUsd([state(0.3, { totalToolDuration: -1 })], id, log)).toBe(0)
    expect(restoredCostUsd([state(0.3, { totalCostUSD: '0.3' })], id, log)).toBe(0)
    expect(restoredCostUsd([state(0.3, { modelUsage: { 'bad\u0000name': {} } })], id, log)).toBe(0)
    expect(logged).toHaveLength(5)
  })
})
