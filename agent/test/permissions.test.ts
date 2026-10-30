import { readFileSync } from 'node:fs'
import type { HookInput } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  decide,
  type InputGuard,
  makeCanUseTool,
  makePreToolUseHook,
  GUARD_DEADLINE_MS,
  judgeInput,
  type LiveSeam,
  makeResultScreenHook,
  RISK_TIERS,
  type RiskTier,
  type ToolDecision,
  uncheckedMessage,
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

  // #4442: a tool that resolves what its arguments name parks the resolved input, with
  // the line that says it, and runs exactly that once approved.
  it('canUseTool parks the input a tool resolves, with its line, and runs that input', async () => {
    const parked: { input?: Record<string, unknown>; summary?: string } = {}
    const prepare = async (name: string, input: Record<string, unknown>) =>
      name === 'mcp__scadbuddy__send_to_printer' ? { input: { ...input, printer_id: 2 }, summary: 'Print on printer "Garage"' } : undefined
    const canUseTool = makeCanUseTool(
      tierOf,
      undefined,
      (request) => {
        parked.input = request.input
        if (request.summary !== undefined) parked.summary = request.summary
        return Promise.resolve({ approved: true, input: request.input })
      },
      undefined,
      prepare,
    )
    expect(await canUseTool('mcp__scadbuddy__send_to_printer', { printer: 'p' }, opts('t5'))).toEqual({
      behavior: 'allow',
      updatedInput: { printer: 'p', printer_id: 2 },
    })
    expect(parked).toEqual({ input: { printer: 'p', printer_id: 2 }, summary: 'Print on printer "Garage"' })
  })

  it("canUseTool resolves under the call's abort signal (#4442 gate review)", async () => {
    let given: AbortSignal | undefined
    const canUseTool = makeCanUseTool(tierOf, undefined, (request) => Promise.resolve({ approved: true, input: request.input }), undefined, async (_name, _input, s) => {
      given = s
      return undefined
    })
    await canUseTool('mcp__scadbuddy__send_to_printer', {}, opts('t7'))
    expect(given).toBe(signal)
  })

  it('canUseTool refuses a call its tool will not resolve, before anything parks', async () => {
    let asked = false
    const canUseTool = makeCanUseTool(
      tierOf,
      undefined,
      () => {
        asked = true
        return Promise.resolve({ approved: true, input: {} })
      },
      undefined,
      async () => {
        throw new Error('Garage is Bambuddy printer 2, not printer_id 3.')
      },
    )
    expect(await canUseTool('mcp__scadbuddy__send_to_printer', { printer: 'p', printer_id: 3 }, opts('t6'))).toEqual({
      behavior: 'deny',
      message: expect.stringContaining('Garage is Bambuddy printer 2, not printer_id 3.'),
    })
    expect(asked).toBe(false)
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

  it('a guard that does not answer in time denies (security review)', async () => {
    const never: InputGuard = () => new Promise(() => {})
    expect(await judgeInput(never, nav, { url: 'https://allowed.example/' }, 20)).toEqual({
      deny: expect.stringMatching(/could not be checked \(no answer in 20 ms\)/) as unknown,
    })
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

// #5018: Claude Code passes over a PreToolUse hook that throws (measured, test/run.test.ts),
// so each seam must answer a deny itself when anything in its decision throws.
describe('a decision that throws fails closed (#5018)', () => {
  const opts = (id: string) => ({ signal, toolUseID: id, requestId: `r-${id}` })
  const tool = 'mcp__scadbuddy__list_models'
  const SECRET = 'input-secret-5018'
  const boom = (): never => {
    throw new TypeError(`bad ${SECRET}`)
  }
  const throwingTier = (name: string) => (name === tool ? boom() : tiers[name])
  const throwingGuard: InputGuard = () => boom()
  const allowAll = async () => undefined
  const throwingLedger: LiveSeam = { access: allowAll, ledger: { record: () => boom(), tierOf: () => undefined } }
  const failingRead: LiveSeam = { access: () => Promise.reject(new Error('db down')) }
  type Case = [string, Parameters<typeof makeCanUseTool>[0], InputGuard | undefined, LiveSeam | undefined]
  const cases: Case[] = [
    ['a throwing tier resolver', throwingTier, undefined, undefined],
    ['a throwing guard', tierOf, throwingGuard, undefined],
    ['a throwing ledger', tierOf, undefined, throwingLedger],
    ['a failed access read whose resolver throws', throwingTier, undefined, failingRead],
  ]
  let logged: string[] = []
  const spy = () => {
    logged = []
    vi.spyOn(console, 'error').mockImplementation((line: string) => void logged.push(line))
  }
  afterEach(() => vi.restoreAllMocks())

  for (const [what, resolver, guard, live] of cases) {
    it(`canUseTool denies a call on ${what}, and reports the deny`, async () => {
      spy()
      const seen: ToolDecision[] = []
      const canUseTool = makeCanUseTool(resolver, (_n, d) => seen.push(d), undefined, guard, undefined, live)
      const answer = await canUseTool(tool, { text: SECRET }, opts('t'))
      expect(answer).toMatchObject({ behavior: 'deny' })
      expect(seen.map((d) => d.decision)).toEqual(['deny'])
      expect(JSON.stringify(logged)).not.toContain(SECRET)
    })

    it(`the hook denies a call on ${what}, and reports the deny`, async () => {
      spy()
      const seen: ToolDecision[] = []
      const hook = makePreToolUseHook(resolver, (_n, d) => seen.push(d), undefined, guard, live)
      const answer = await hook.hooks[0]!({ ...preToolUse(tool), tool_input: { text: SECRET } } as HookInput, 'toolu_1', {
        signal,
      })
      expect(answer).toMatchObject({ hookSpecificOutput: { permissionDecision: 'deny' } })
      expect(seen.map((d) => d.decision)).toEqual(['deny'])
      expect(JSON.stringify(logged)).not.toContain(SECRET)
    })
  }

  it('says why, and logs only the error type', async () => {
    spy()
    const canUseTool = makeCanUseTool(throwingTier)
    expect(await canUseTool(tool, { text: SECRET }, opts('m'))).toEqual({ behavior: 'deny', message: uncheckedMessage(tool) })
    expect(uncheckedMessage(tool)).toBe(`${tool} is refused: its access could not be checked.`)
    expect(logged).toEqual([`canUseTool: deciding ${tool} failed (TypeError); the call is refused`])
    const hook = makePreToolUseHook(throwingTier)
    expect(await hook.hooks[0]!(preToolUse(tool), 'toolu_1', { signal })).toEqual({
      hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: uncheckedMessage(tool) },
    })
  })

  it('a listener that throws refuses an allowed call in both seams', async () => {
    spy()
    const canUseTool = makeCanUseTool(tierOf, () => boom())
    expect(await canUseTool(tool, {}, opts('l'))).toEqual({ behavior: 'deny', message: uncheckedMessage(tool) })
    const hook = makePreToolUseHook(tierOf, () => boom())
    expect(await hook.hooks[0]!(preToolUse('mcp__scadbuddy__send_to_printer'), 'toolu_1', { signal })).toMatchObject({
      hookSpecificOutput: { permissionDecision: 'deny', permissionDecisionReason: uncheckedMessage('mcp__scadbuddy__send_to_printer') },
    })
    expect(JSON.stringify(logged)).not.toContain(SECRET)
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
    expect(texts).toHaveLength(2)
    expect(texts[0]).toContain('Page URL: https://evil.example/')
  })

  it('reads a page named anywhere in the response, whatever its shape (security review)', async () => {
    const hook = makeResultScreenHook((_n, _i, text) => Promise.resolve(text.includes('evil') ? 'withheld' : undefined), covers)
    const run = (response: unknown) => hook.hooks[0]!(postToolUse('mcp__playwright__browser_click', response), 'toolu_1', { signal })
    for (const response of [
      { content: [{ type: 'text', text: 'Page URL: https://evil.example/' }] },
      'Page URL: https://evil.example/',
      [{ type: 'resource', resource: { uri: 'x', text: 'Page URL: https://evil.example/' } }],
    ]) {
      expect(await run(response)).toMatchObject({ hookSpecificOutput: { updatedMCPToolOutput: [{ text: 'withheld' }] } })
    }
  })

  it('withholds the result when the screen does not answer in time, and outlasts it with its own hook timeout', async () => {
    const hook = makeResultScreenHook(() => new Promise<undefined>(() => {}), covers, 20)
    expect(hook.timeout).toBeGreaterThan(GUARD_DEADLINE_MS / 1000)
    expect(await hook.hooks[0]!(postToolUse('mcp__playwright__browser_snapshot', []), 'toolu_1', { signal })).toMatchObject({
      hookSpecificOutput: { updatedMCPToolOutput: [{ type: 'text', text: expect.stringMatching(/withheld.*no answer in 20 ms/) as unknown }] },
    })
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
