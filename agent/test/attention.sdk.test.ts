import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { query, type SDKMessage, type SDKResultMessage, type SDKSystemMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  ATTENTION_DESCRIPTION,
  answeredText,
  attentionCard,
  attentionHandler,
  attentionSpec,
  DEFAULT_REPLIES,
  DEFAULT_TIMEOUT_S,
  DONE_REPLIES,
  parseAttention,
  POSTED_TEXT,
  RECONNECTED_TEXT,
  timedOutText,
  WAIT_CEILING_S,
} from '../src/harness/attention.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ATTENTION_TOOL, type QuestionGate, type QuestionRequest } from '../src/harness/questions.js'
import { buildHarnessOptions, type HarnessRun } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// #815: request_user_attention, an `answer`-kind entry at the question gate.
// Its input rules and handler here, then the real SDK and its
// bundled binary against the local fake Anthropic endpoint. Nothing reaches
// Anthropic. The Postgres side is test/attention.pg.test.ts.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const GATEWAY_TOKEN = 'gw-attention-test-token-0000111122223333'
const MESSAGE = 'The ScadBuddy tab closed. Reopen it so I can select the plate?'

describe('parseAttention', () => {
  it("applies #815's defaults: 300 s, then proceed", () => {
    const parsed = parseAttention({ reason: 'tab_disconnected', message: MESSAGE })
    expect(parsed).toEqual({ ok: true, input: { reason: 'tab_disconnected', message: MESSAGE, timeout_s: DEFAULT_TIMEOUT_S, on_timeout: 'proceed' } })
  })

  it("refuses 'approval_pending': an approval is its own entry and never times out to proceed", () => {
    expect(parseAttention({ reason: 'approval_pending', message: 'x', on_timeout: 'wait' })).toMatchObject({
      ok: false,
      error: expect.stringMatching(/approval_pending.*own approval/),
    })
  })

  it('refuses a timeout out of bounds rather than clamping it, unknown fields, and repeated options', () => {
    for (const bad of [
      { timeout_s: 5 },
      { timeout_s: 86_401 },
      { timeout_s: 1.5 },
      { on_timeout: 'approve' },
      { options: ['Only one'] },
      { options: ['Same', 'Same'] },
      { message: '   ' },
      { answer: 'yes' },
    ]) {
      expect(parseAttention({ reason: 'blocked', message: MESSAGE, ...bad }).ok, JSON.stringify(bad)).toBe(false)
    }
  })

  it("'wait' parks to the day-long ceiling, whatever timeout_s said; the others use timeout_s", () => {
    const at = (on_timeout: string) => {
      const parsed = parseAttention({ reason: 'blocked', message: MESSAGE, timeout_s: 60, on_timeout })
      if (!parsed.ok) throw new Error(parsed.error)
      const spec = attentionSpec(parsed.input)
      if (spec.reason === 'done') throw new Error('a blocked request has a timer')
      return spec.timeoutS
    }
    expect(at('wait')).toBe(WAIT_CEILING_S)
    expect(at('proceed')).toBe(60)
    expect(at('stop')).toBe(60)
  })

  // #815 §4: a done summary is posted, never waited on.
  it("'done' has no timer and Dismiss replies, and refuses what only a wait would use", () => {
    const parsed = parseAttention({ reason: 'done', message: MESSAGE })
    if (!parsed.ok) throw new Error(parsed.error)
    expect(attentionSpec(parsed.input)).toEqual({ reason: 'done' })
    expect(attentionCard(parsed.input)).toMatchObject({ header: 'Done', options: DONE_REPLIES.map((label) => ({ label })) })
    for (const extra of [{ options: ['a', 'b'] }, { timeout_s: 60 }, { on_timeout: 'wait' }]) {
      const refused = parseAttention({ reason: 'done', message: MESSAGE, ...extra })
      expect(refused, JSON.stringify(extra)).toMatchObject({ ok: false, error: expect.stringMatching(/does not wait for a reply/) })
    }
  })
})

describe('ATTENTION_DESCRIPTION', () => {
  it("does not send the model to it after a failed browser_* call, which waits for the tab itself (#815)", () => {
    expect(ATTENTION_DESCRIPTION).toMatch(/Do not use it after a browser_\* call finds no tab: that call already waits/)
    expect(ATTENTION_DESCRIPTION).not.toMatch(/`tab_disconnected` after a browser_\* call/)
    expect(ATTENTION_DESCRIPTION).not.toMatch(/One request per reason/)
  })
})

describe('request_user_attention handler', () => {
  const extra = (meta: Record<string, unknown> | null = { 'claudecode/toolUseId': 'toolu_att1' }) => ({
    signal: new AbortController().signal,
    ...(meta ? { _meta: meta } : {}),
  })
  const recording = (verdict: () => ReturnType<QuestionGate>) => {
    const asked: QuestionRequest[] = []
    const gate: QuestionGate = (request) => {
      asked.push(request)
      return verdict()
    }
    return { asked, gate }
  }

  it('parks one card with the default quick replies and the timer spec, and returns the reply', async () => {
    const { asked, gate } = recording(() => Promise.resolve({ answered: true, answers: { [MESSAGE]: "I'm here" } }))
    const result = await attentionHandler(gate, ATTENTION_TOOL, { reason: 'tab_disconnected', message: MESSAGE }, extra())
    expect(asked).toMatchObject([
      {
        tool: ATTENTION_TOOL,
        toolUseId: 'toolu_att1',
        questions: [{ question: MESSAGE, header: 'Tab disconnected', multiSelect: false, options: DEFAULT_REPLIES.map((label) => ({ label })) }],
        attention: { reason: 'tab_disconnected', onTimeout: 'proceed', timeoutS: 300 },
      },
    ])
    expect(result).toEqual({ content: [{ type: 'text', text: answeredText("I'm here") }] })
  })

  it('a timed-out proceed is a result, not an error, and says nothing was approved', async () => {
    const { gate } = recording(() => Promise.resolve({ answered: false, timedOut: true, message: 'nobody replied' }))
    const result = await attentionHandler(gate, ATTENTION_TOOL, { reason: 'blocked', message: MESSAGE, timeout_s: 30 }, extra())
    expect(result).toEqual({ content: [{ type: 'text', text: timedOutText(30) }] })
    expect(timedOutText(30)).toMatch(/^timed_out: .*never approves anything/)
  })

  // #1343: only a read is re-run when the tab is back; the model must not repeat a write blind.
  it('a reconnect is a result, not an error, and sends the model to re-check the page, not to retry', async () => {
    const { gate } = recording(() => Promise.resolve({ answered: false, reconnected: true, message: 'the ScadBuddy tab is connected again' }))
    const result = await attentionHandler(gate, ATTENTION_TOOL, { reason: 'tab_disconnected', message: MESSAGE }, extra())
    expect(result).toEqual({ content: [{ type: 'text', text: RECONNECTED_TEXT }] })
    expect(RECONNECTED_TEXT).toMatch(/^reconnected: .*re-check it \(browser_status, then browser_snapshot\)/)
    expect(RECONNECTED_TEXT).not.toMatch(/retry/i)
  })

  it('a posted done summary is a result, not an error, and tells the model nobody waits on it', async () => {
    const { asked, gate } = recording(() => Promise.resolve({ answered: false, posted: true, message: 'posted' }))
    const result = await attentionHandler(gate, ATTENTION_TOOL, { reason: 'done', message: MESSAGE }, extra())
    expect(asked).toMatchObject([{ attention: { reason: 'done' }, questions: [{ header: 'Done' }] }])
    expect(result).toEqual({ content: [{ type: 'text', text: POSTED_TEXT }] })
    expect(POSTED_TEXT).toMatch(/^posted: .*no reply will reach you/)
  })

  it('anything else is the error the model reads, and malformed input parks nothing', async () => {
    const { asked, gate } = recording(() => Promise.resolve({ answered: false, message: 'The user did not answer: the turn stopped first.' }))
    expect(await attentionHandler(gate, ATTENTION_TOOL, { reason: 'done', message: MESSAGE }, extra())).toEqual({
      content: [{ type: 'text', text: 'The user did not answer: the turn stopped first.' }],
      isError: true,
    })
    const bad = await attentionHandler(gate, ATTENTION_TOOL, { reason: 'approval_pending', message: MESSAGE }, extra())
    expect(bad.isError).toBe(true)
    expect(await attentionHandler(gate, ATTENTION_TOOL, { reason: 'done', message: MESSAGE }, extra(null))).toMatchObject({ isError: true })
    expect(asked).toHaveLength(1)

    const thrown = await attentionHandler(() => Promise.reject(new Error('db down')), ATTENTION_TOOL, { reason: 'done', message: MESSAGE }, extra())
    expect(thrown).toMatchObject({ isError: true, content: [{ text: expect.stringContaining('could not complete (db down)') }] })
  })
})

describe.skipIf(cliMissing !== undefined)(`request_user_attention through the question gate${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let stateDir: string

  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'attention-sdk-'))
    await ensureStateDirs({ stateDir })
    fake = await startFakeAnthropic((r) => script(r))
  })
  afterEach(async () => {
    await fake.close()
  })

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
  const callScript = (input: Record<string, unknown>, after: string) => (r: RecordedRequest) =>
    lastContent(r).includes('tool_result') ? { text: after } : { toolUse: { name: ATTENTION_TOOL, input } }

  async function collect(run: Omit<HarnessRun, 'paths' | 'credential'>) {
    const options = buildHarnessOptions({
      paths: { stateDir },
      credential: { kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN },
      model: 'claude-sonnet-4-5',
      ...run,
    })
    const messages: SDKMessage[] = []
    for await (const m of query({ prompt: run.prompt, options })) messages.push(m)
    const init = messages.find((m): m is SDKSystemMessage => m.type === 'system' && m.subtype === 'init')
    const result = messages.find((m): m is SDKResultMessage => m.type === 'result')
    return { init, result }
  }

  it('is offered with a gate, parks the call at read tier (no approval), and the reply reaches the model', async () => {
    script = callScript({ reason: 'tab_disconnected', message: MESSAGE, options: ['Reconnected', 'Go ahead without me'] }, 'Thanks.')
    const asked: QuestionRequest[] = []
    const { init, result } = await collect({
      prompt: 'Select the plate',
      questionGate: (request) => {
        asked.push(request)
        return Promise.resolve({ answered: true, answers: { [MESSAGE]: 'Reconnected' } })
      },
    })
    expect(init?.tools).toContain(ATTENTION_TOOL)
    expect(asked).toHaveLength(1)
    expect(asked[0]?.toolUseId).toMatch(/^toolu_/)
    expect(asked[0]?.attention).toEqual({ reason: 'tab_disconnected', onTimeout: 'proceed', timeoutS: 300 })
    expect(result).toMatchObject({ subtype: 'success', result: 'Thanks.' })
    expect(result?.permission_denials).toEqual([])
    const followUp = lastContent(fake.messageCalls().at(-1)!)
    expect(followUp).toContain('answered: the user replied')
    expect(followUp).toContain('Reconnected')
    expect(followUp).not.toMatch(/"is_error":true/)
  }, 60_000)

  it('a proceed timeout reaches the model as timed_out, not as an error and not as a reply', async () => {
    script = callScript({ reason: 'blocked', message: MESSAGE, timeout_s: 10 }, 'Carrying on headless.')
    const { result } = await collect({
      prompt: 'go',
      questionGate: () => Promise.resolve({ answered: false, timedOut: true, message: 'nobody replied' }),
    })
    expect(result?.subtype).toBe('success')
    const followUp = lastContent(fake.messageCalls().at(-1)!)
    expect(followUp).toContain('timed_out: the user did not reply within 10 s')
    expect(followUp).not.toContain('answered:')
    expect(followUp).not.toMatch(/"is_error":true/)
  }, 60_000)

  it("refuses 'approval_pending' as the tool's error without parking anything", async () => {
    script = callScript({ reason: 'approval_pending', message: MESSAGE, on_timeout: 'proceed' }, 'ok')
    const asked: QuestionRequest[] = []
    const { result } = await collect({
      prompt: 'go',
      questionGate: (request) => {
        asked.push(request)
        return Promise.resolve({ answered: true, answers: {} })
      },
    })
    expect(asked).toEqual([])
    expect(result?.subtype).toBe('success')
    expect(lastContent(fake.messageCalls().at(-1)!)).toMatch(/"is_error":true/)
  }, 60_000)

  it('without a gate the tool is not offered', async () => {
    script = () => ({ text: 'Nothing to ask.' })
    const { init } = await collect({ prompt: 'go' })
    expect(init?.tools).not.toContain(ATTENTION_TOOL)
  }, 60_000)
})
