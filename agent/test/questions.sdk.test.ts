import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { query, type SDKMessage, type SDKResultMessage, type SDKSystemMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ASK_USER_QUESTION, parseQuestions, type QuestionGate, type QuestionRequest } from '../src/harness/questions.js'
import { buildHarnessOptions, type HarnessRun } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// #940: the agent asks the user structured questions with Claude Code's own
// AskUserQuestion tool, which the SDK hands to `canUseTool`; the host answers
// by allowing the call with `answers` added to its input. Measured here
// against the real SDK (0.3.283) and its bundled binary, pointed at the local
// fake Anthropic endpoint. Nothing reaches Anthropic.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const GATEWAY_TOKEN = 'gw-questions-test-token-0000111122223333'

const QUESTIONS = [
  {
    question: 'Which colour should the base be?',
    header: 'Colour',
    multiSelect: false,
    options: [
      { label: 'Red', description: 'PLA Basic red' },
      { label: 'Blue', description: 'PLA Basic blue', preview: '## Draft\n\nA **blue** base.' },
    ],
  },
]

describe('parseQuestions', () => {
  it('refuses two options with the same label, or two questions with the same text', () => {
    const q = (labels: string[], question = 'Pick one?') => ({
      question,
      header: 'H',
      multiSelect: false,
      options: labels.map((label) => ({ label, description: '' })),
    })
    expect(parseQuestions({ questions: [q(['Yes', 'No'])] }).ok).toBe(true)
    expect(parseQuestions({ questions: [q(['Yes', 'Yes'])] })).toMatchObject({ ok: false, error: expect.stringMatching(/own label/) })
    expect(parseQuestions({ questions: [q(['Yes', 'No']), q(['A', 'B'])] }).ok).toBe(false)
    // A multi-select answer joins labels with ", ", so none may hold a comma; a single choice may.
    const multi = { ...q(['Red, matte', 'Blue']), multiSelect: true }
    expect(parseQuestions({ questions: [multi] })).toMatchObject({ ok: false, error: expect.stringMatching(/comma/) })
    expect(parseQuestions({ questions: [q(['Red, matte', 'Blue'])] }).ok).toBe(true)
  })
})

describe.skipIf(cliMissing !== undefined)(`AskUserQuestion through the question gate${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let stateDir: string

  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'questions-sdk-'))
    await ensureStateDirs({ stateDir })
    fake = await startFakeAnthropic((r) => script(r))
  })
  afterEach(async () => {
    await fake.close()
  })

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
  const askScript = (input: Record<string, unknown>, after: string) => (r: RecordedRequest) =>
    lastContent(r).includes('tool_result') ? { text: after } : { toolUse: { name: ASK_USER_QUESTION, input } }

  async function collect(run: Omit<HarnessRun, 'paths' | 'credential'>) {
    const options = buildHarnessOptions({
      paths: { stateDir },
      credential: { kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN },
      model: 'claude-sonnet-4-5',
      ...run,
    })
    const messages: SDKMessage[] = []
    let error: unknown
    try {
      for await (const m of query({ prompt: run.prompt, options })) messages.push(m)
    } catch (err) {
      error = err
    }
    const init = messages.find((m): m is SDKSystemMessage => m.type === 'system' && m.subtype === 'init')
    const result = messages.find((m): m is SDKResultMessage => m.type === 'result')
    return { init, result, error }
  }

  it('parks the call on the gate and hands the answers back to the model as the tool result', async () => {
    script = askScript({ questions: QUESTIONS }, 'Blue it is.')
    const asked: QuestionRequest[] = []
    const gate: QuestionGate = (request) => {
      asked.push(request)
      return Promise.resolve({ answered: true, answers: { 'Which colour should the base be?': 'Blue' } })
    }
    const { init, result, error } = await collect({ prompt: 'Ask me', questionGate: gate })
    expect(error).toBeUndefined()
    expect(init?.tools).toContain(ASK_USER_QUESTION)
    expect(asked).toHaveLength(1)
    expect(asked[0]?.questions).toEqual(QUESTIONS)
    expect(asked[0]?.toolUseId).toMatch(/^toolu_/)
    expect(result).toMatchObject({ subtype: 'success', result: 'Blue it is.' })
    expect(result?.permission_denials).toEqual([])
    const followUp = lastContent(fake.messageCalls().at(-1)!)
    expect(followUp).toContain('Which colour should the base be?')
    expect(followUp).toContain('Blue')
    expect(followUp).not.toMatch(/"is_error":true/)
  }, 60_000)

  it('free text the user typed instead of an option reaches the model as the answer', async () => {
    script = askScript({ questions: QUESTIONS }, 'Green, then.')
    const { result } = await collect({
      prompt: 'Ask me',
      questionGate: () => Promise.resolve({ answered: true, answers: { 'Which colour should the base be?': 'Make it green' } }),
    })
    expect(result?.subtype).toBe('success')
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('Make it green')
  }, 60_000)

  it('an unanswered question reaches the model as the tool error, never as an answer', async () => {
    script = askScript({ questions: QUESTIONS }, 'No answer, so I stop.')
    const { result } = await collect({
      prompt: 'Ask me',
      questionGate: () => Promise.resolve({ answered: false, message: 'The user did not answer: the turn was interrupted.' }),
    })
    expect(result?.permission_denials.map((d) => d.tool_name)).toEqual([ASK_USER_QUESTION])
    const followUp = lastContent(fake.messageCalls().at(-1)!)
    expect(followUp).toContain('The user did not answer')
    expect(followUp).toMatch(/"is_error":true/)
  }, 60_000)

  it('refuses input that is not a well-formed question without asking anyone', async () => {
    script = askScript({ questions: [{ question: 'Pick?', header: 'H', multiSelect: false, options: [] }] }, 'ok')
    const asked: QuestionRequest[] = []
    const { result } = await collect({
      prompt: 'Ask me',
      questionGate: (request) => {
        asked.push(request)
        return Promise.resolve({ answered: true, answers: {} })
      },
    })
    expect(asked).toEqual([])
    expect(lastContent(fake.messageCalls().at(-1)!)).toMatch(/"is_error":true/)
    expect(result?.subtype).toBe('success')
  }, 60_000)

  it('without a gate the tool is not offered at all', async () => {
    script = () => ({ text: 'Nothing to ask.' })
    const { init } = await collect({ prompt: 'Ask me' })
    expect(init?.tools).not.toContain(ASK_USER_QUESTION)
  }, 60_000)
})
