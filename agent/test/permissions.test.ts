import { readFileSync } from 'node:fs'
import type { HookInput } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import {
  decide,
  type InputGuard,
  makeCanUseTool,
  makePreToolUseHook,
  makeResultScreenHook,
  RISK_TIERS,
  type RiskTier,
  type ToolDecision,
} from '../src/harness/permissions.js'

const tiers: Record<string, RiskTier> = {
  mcp__scadbuddy__list_models: 'read',
  mcp__scadbuddy__save_model: 'write',
  mcp__scadbuddy__send_to_printer: 'outward',
}
const tierOf = (name: string) => tiers[name]
const signal = new AbortController().signal

function preToolUse(toolName: string): HookInput {
  return {
    hook_event_name: 'PreToolUse',
    tool_name: toolName,
    tool_input: {},
    tool_use_id: 'toolu_1',
    session_id: 's',
    transcript_path: '/dev/null',
    cwd: '/tmp',
  } as HookInput
}

describe('tier → decision', () => {
  it('allows read and write, and holds outward for approval', () => {
    expect(decide('mcp__scadbuddy__list_models', tierOf)).toEqual({ decision: 'allow', tier: 'read' })
    expect(decide('mcp__scadbuddy__save_model', tierOf)).toEqual({ decision: 'allow', tier: 'write' })
    const outward = decide('mcp__scadbuddy__send_to_printer', tierOf)
    expect(outward.decision).toBe('needs_approval')
    expect(outward.tier).toBe('outward')
  })

  it('treats a tool it does not know as outward (spec §8.1)', () => {
    expect(decide('mcp__someplugin__do_things', tierOf)).toMatchObject({ decision: 'needs_approval', tier: 'outward' })
    expect(decide('Bash', tierOf)).toMatchObject({ decision: 'needs_approval', tier: 'outward' })
  })
})

describe('canUseTool', () => {
  it('allows with the input unchanged, and denies outward with the approval message', async () => {
    const seen: [string, ToolDecision][] = []
    const canUseTool = makeCanUseTool(tierOf, (name, d) => seen.push([name, d]))
    expect(await canUseTool('mcp__scadbuddy__save_model', { slug: 'box' }, { signal, toolUseID: 't1', requestId: 'r1' })).toEqual({
      behavior: 'allow',
      updatedInput: { slug: 'box' },
    })
    const denied = await canUseTool('mcp__scadbuddy__send_to_printer', {}, { signal, toolUseID: 't2', requestId: 'r2' })
    expect(denied).toEqual({ behavior: 'deny', message: expect.stringMatching(/needs a human approval.*#258/) })
    expect(seen.map(([name, d]) => [name, d.decision])).toEqual([
      ['mcp__scadbuddy__save_model', 'allow'],
      ['mcp__scadbuddy__send_to_printer', 'needs_approval'],
    ])
  })
})

describe('PreToolUse hook', () => {
  const hook = makePreToolUseHook(tierOf)
  const run = (name: string) => hook.hooks[0]!(preToolUse(name), 'toolu_1', { signal })

  it('passes allowed tools on without a verdict, so canUseTool decides', async () => {
    expect(await run('mcp__scadbuddy__list_models')).toEqual({})
  })

  it('denies outward and unknown tools before any other permission step', async () => {
    for (const name of ['mcp__scadbuddy__send_to_printer', 'mcp__someplugin__do_things']) {
      expect(await run(name)).toMatchObject({
        hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny' },
      })
    }
  })
})

describe('with an approval gate (#258)', () => {
  const opts = (id: string) => ({ signal, toolUseID: id, requestId: `r-${id}` })

  it('canUseTool parks outward calls on the gate and runs the approved input', async () => {
    const asked: string[] = []
    const canUseTool = makeCanUseTool(tierOf, undefined, (request) => {
      asked.push(`${request.toolName}:${request.toolUseId}:${request.tier}`)
      return Promise.resolve({ approved: true, input: { job: 'approved' } })
    })
    expect(await canUseTool('mcp__scadbuddy__send_to_printer', { job: 'asked' }, opts('t1'))).toEqual({
      behavior: 'allow',
      updatedInput: { job: 'approved' },
    })
    // Read and write never reach the gate.
    expect(await canUseTool('mcp__scadbuddy__list_models', {}, opts('t2'))).toMatchObject({ behavior: 'allow' })
    expect(asked).toEqual(['mcp__scadbuddy__send_to_printer:t1:outward'])
  })

  it('a refusal or a failing gate is a deny', async () => {
    const refused = makeCanUseTool(tierOf, undefined, () => Promise.resolve({ approved: false, message: 'The user denied it.' }))
    expect(await refused('mcp__scadbuddy__send_to_printer', {}, opts('t3'))).toEqual({
      behavior: 'deny',
      message: 'The user denied it.',
    })
    const broken = makeCanUseTool(tierOf, undefined, () => Promise.reject(new Error('database down')))
    expect(await broken('mcp__scadbuddy__send_to_printer', {}, opts('t4'))).toEqual({
      behavior: 'deny',
      message: expect.stringMatching(/could not complete \(database down\)/),
    })
  })

  it('the hook forces the prompt (ask) for outward calls instead of denying them', async () => {
    const hook = makePreToolUseHook(tierOf, undefined, () => Promise.resolve({ approved: false, message: 'no' }))
    const run = (name: string) => hook.hooks[0]!(preToolUse(name), 'toolu_1', { signal })
    expect(await run('mcp__scadbuddy__list_models')).toEqual({})
    expect(await run('mcp__scadbuddy__send_to_printer')).toMatchObject({
      hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'ask' },
    })
  })
})

describe('an input guard (#982)', () => {
  const opts = (id: string) => ({ signal, toolUseID: id, requestId: `r-${id}` })
  const nav = 'mcp__playwright__browser_navigate'
  // Async, as the browser's guard is: it reads the lists from the database.
  const guard: InputGuard = (_name, input) => {
    const url = (input as { url?: string }).url
    if (url === 'https://denied.example/') return Promise.resolve({ deny: 'refused by "denied.example"' })
    if (url === 'https://allowed.example/') return Promise.resolve({ allow: 'allowed' })
    if (url === 'https://broken.example/') return Promise.reject(new Error('database down'))
    return Promise.resolve(url === undefined ? undefined : { outward: 'not on the allow list' })
  }
  const gate = () => Promise.resolve({ approved: true as const, input: {} })

  it('runs an outward tool unasked when the guard allows its input, and asks for one it does not', async () => {
    const seen: ToolDecision[] = []
    const canUseTool = makeCanUseTool(tierOf, (_n, d) => seen.push(d), undefined, guard)
    expect(await canUseTool(nav, { url: 'https://allowed.example/' }, opts('a'))).toEqual({
      behavior: 'allow',
      updatedInput: { url: 'https://allowed.example/' },
    })
    expect(await canUseTool(nav, { url: 'https://other.example/' }, opts('b'))).toMatchObject({
      behavior: 'deny',
      message: expect.stringMatching(/not on the allow list.*no approval surface/) as unknown,
    })
    expect(seen.map((d) => [d.decision, d.tier])).toEqual([
      ['allow', 'write'],
      ['needs_approval', 'outward'],
    ])
  })

  it('a denied input is refused even with a gate that would approve, and a failing guard denies', async () => {
    const canUseTool = makeCanUseTool(tierOf, undefined, gate, guard)
    expect(await canUseTool(nav, { url: 'https://denied.example/' }, opts('c'))).toEqual({
      behavior: 'deny',
      message: 'refused by "denied.example"',
    })
    expect(await canUseTool(nav, { url: 'https://broken.example/' }, opts('d'))).toEqual({
      behavior: 'deny',
      message: expect.stringMatching(/could not be checked \(database down\)/) as unknown,
    })
    const hook = makePreToolUseHook(tierOf, undefined, gate, guard)
    const run = (url: string) =>
      hook.hooks[0]!({ ...preToolUse(nav), tool_input: { url } } as HookInput, 'toolu_1', { signal })
    expect(await run('https://denied.example/')).toMatchObject({ hookSpecificOutput: { permissionDecision: 'deny' } })
    expect(await run('https://other.example/')).toMatchObject({ hookSpecificOutput: { permissionDecision: 'ask' } })
    expect(await run('https://allowed.example/')).toEqual({})
  })
})

describe('the result screen hook (#982)', () => {
  const postToolUse = (name: string, response: unknown) =>
    ({
      hook_event_name: 'PostToolUse',
      tool_name: name,
      tool_input: { url: 'https://a.example/' },
      tool_response: response,
      tool_use_id: 'toolu_1',
      session_id: 's',
      transcript_path: '/dev/null',
      cwd: '/tmp',
    }) as HookInput
  const covers = (name: string) => name.startsWith('mcp__playwright__')

  it("replaces a covered tool's content blocks with the screen's text, and leaves the rest alone", async () => {
    const texts: string[] = []
    const hook = makeResultScreenHook((_n, _i, text) => {
      texts.push(text)
      return Promise.resolve(text.includes('evil') ? 'withheld' : undefined)
    }, covers)
    const run = (name: string, response: unknown) => hook.hooks[0]!(postToolUse(name, response), 'toolu_1', { signal })
    expect(await run('mcp__playwright__browser_click', [{ type: 'text', text: 'Page URL: https://evil.example/' }])).toEqual({
      hookSpecificOutput: { hookEventName: 'PostToolUse', updatedMCPToolOutput: [{ type: 'text', text: 'withheld' }] },
    })
    expect(await run('mcp__playwright__browser_snapshot', [{ type: 'text', text: 'Page URL: https://a.example/' }])).toEqual({})
    expect(await run('mcp__other__browser_click', [{ type: 'text', text: 'Page URL: https://evil.example/' }])).toEqual({})
    expect(texts).toEqual(['Page URL: https://evil.example/', 'Page URL: https://a.example/'])
  })

  it('withholds the result when the screen fails', async () => {
    const hook = makeResultScreenHook(() => Promise.reject(new Error('database down')), covers)
    expect(await hook.hooks[0]!(postToolUse('mcp__playwright__browser_snapshot', []), 'toolu_1', { signal })).toMatchObject({
      hookSpecificOutput: { updatedMCPToolOutput: [{ type: 'text', text: expect.stringMatching(/withheld.*database down/) as unknown }] },
    })
  })
})

describe('RISK_TIERS', () => {
  it('is the vector agent-durable pins its copy to (#3144)', () => {
    const fixture = JSON.parse(readFileSync(new URL('./fixtures/risk-tiers.json', import.meta.url), 'utf8')) as unknown
    expect(fixture).toEqual([...RISK_TIERS])
  })
})
