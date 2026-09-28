import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createSdkMcpServer, query, type SDKMessage, type SDKResultMessage, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { ApprovalGate, ApprovalRequest, ApprovalVerdict, RiskTier } from '../src/harness/permissions.js'
import { buildHarnessOptions, type HarnessRun } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// #258, spec §3.2 → §3.1: can `canUseTool` park an outward call on an
// asynchronous human decision, with no deadline of the SDK's own? Measured
// here against the real SDK (0.3.283) and its bundled Claude Code binary,
// pointed at the local fake Anthropic endpoint as a gateway (as
// test/run.test.ts does). Nothing reaches Anthropic.
//
// The remote-dialog deadline (`dialogExpiry`, default 5m, overridable by
// CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS) is set to 1 s in the Claude Code
// process, and the call is parked for several times that: sdk.d.ts says
// "Local-only permission prompts (no remote client) are unaffected", and this
// shows it.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const GATEWAY_TOKEN = 'gw-approvals-test-token-0000111122223333'
// SCADBUDDY_APPROVAL_PARK_MS=330000 parks for longer than the 5-minute
// default instead, with that default left in place (not run in CI; the PR
// that moved §3.2's row records the run).
const LONG_PARK_MS = Number(process.env.SCADBUDDY_APPROVAL_PARK_MS) || undefined
const PARK_MS = LONG_PARK_MS ?? 5_000
const DIALOG_TIMEOUT_MS = LONG_PARK_MS ? undefined : 1_000

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void }
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

describe.skipIf(cliMissing !== undefined)(`canUseTool parks outward calls${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let stateDir: string
  let handled: string[]

  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'approvals-sdk-'))
    await ensureStateDirs({ stateDir })
    fake = await startFakeAnthropic((r) => script(r))
    handled = []
  })
  afterEach(async () => {
    await fake.close()
  })

  function stubServer() {
    const print = tool('print', 'Send to the printer', { job: z.string() }, (args) => {
      handled.push(`print:${args.job}`)
      return Promise.resolve({ content: [{ type: 'text' as const, text: `printing ${args.job}` }] })
    })
    return createSdkMcpServer({ name: 'stub', tools: [print] })
  }
  const tiers: Record<string, RiskTier> = { mcp__stub__print: 'outward' }
  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  /** Runs through the real `query()` with the harness's own options, plus the short dialog deadline. */
  async function collect(run: Omit<HarnessRun, 'paths' | 'credential'>): Promise<{ messages: SDKMessage[]; result?: SDKResultMessage; error?: unknown }> {
    const options = buildHarnessOptions({
      paths: { stateDir },
      credential: { kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN },
      model: 'claude-sonnet-4-5',
      mcpServers: { stub: stubServer() },
      tierOf: (name) => tiers[name],
      ...run,
    })
    if (DIALOG_TIMEOUT_MS !== undefined) {
      options.env = { ...options.env, CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS: String(DIALOG_TIMEOUT_MS) }
    }
    const messages: SDKMessage[] = []
    try {
      for await (const m of query({ prompt: run.prompt, options })) messages.push(m)
    } catch (error) {
      return { messages, error }
    }
    return { messages, result: messages.find((m): m is SDKResultMessage => m.type === 'result') }
  }

  const printScript = (after: string) => (r: RecordedRequest) =>
    lastContent(r).includes('tool_result') ? { text: after } : { toolUse: { name: 'mcp__stub__print', input: { job: 'box.3mf' } } }

  it('waits for the decision with no deadline of its own, then runs the approved call', async () => {
    script = printScript('Printed.')
    const asked: ApprovalRequest[] = []
    const decision = deferred<ApprovalVerdict>()
    const parked = deferred<void>()
    const gate: ApprovalGate = (request) => {
      asked.push(request)
      parked.resolve()
      return decision.promise
    }
    const running = collect({ prompt: 'Print the box', approvalGate: gate })

    await parked.promise
    const callsWhenParked = fake.messageCalls().length
    await sleep(PARK_MS)
    // Still parked, well past the remote-dialog deadline: nothing ran and the
    // model was sent nothing.
    expect(handled).toEqual([])
    expect(fake.messageCalls()).toHaveLength(callsWhenParked)
    expect(asked).toHaveLength(1)
    expect(asked[0]).toMatchObject({ toolName: 'mcp__stub__print', input: { job: 'box.3mf' }, tier: 'outward' })
    expect(asked[0]?.toolUseId).toMatch(/^toolu_/)
    expect(asked[0]?.signal.aborted).toBe(false)

    decision.resolve({ approved: true, input: { job: 'box.3mf' } })
    const { result, error } = await running
    expect(error).toBeUndefined()
    expect(handled).toEqual(['print:box.3mf'])
    expect(result).toMatchObject({ subtype: 'success', result: 'Printed.' })
    expect(result?.permission_denials).toEqual([])
    // The tool's own result went back to the model.
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('printing box.3mf')
  }, PARK_MS + 60_000)

  it('runs the tool with the input the gate returns (the approved one)', async () => {
    script = printScript('Printed.')
    const { result } = await collect({
      prompt: 'Print the box',
      approvalGate: () => Promise.resolve({ approved: true, input: { job: 'approved.3mf' } }),
    })
    expect(result?.subtype).toBe('success')
    expect(handled).toEqual(['print:approved.3mf'])
  }, 60_000)

  it('a denial reaches the model as the tool error, and the tool never runs', async () => {
    script = printScript('I will not print.')
    const { result } = await collect({
      prompt: 'Print the box',
      approvalGate: () => Promise.resolve({ approved: false, message: 'The user denied printing box.3mf.' }),
    })
    expect(handled).toEqual([])
    expect(result).toMatchObject({ subtype: 'success', result: 'I will not print.' })
    expect(result?.permission_denials.map((d) => d.tool_name)).toEqual(['mcp__stub__print'])
    const followUp = lastContent(fake.messageCalls().at(-1)!)
    expect(followUp).toContain('The user denied printing box.3mf.')
    expect(followUp).toMatch(/"is_error":true/)
  }, 60_000)

  it('aborting the query while parked signals the gate and stops without running the tool', async () => {
    script = printScript('unreachable')
    const stop = new AbortController()
    const parked = deferred<AbortSignal>()
    const gate: ApprovalGate = (request) => {
      parked.resolve(request.signal)
      return new Promise<ApprovalVerdict>(() => {})
    }
    const running = collect({ prompt: 'Print the box', approvalGate: gate, signal: stop.signal })
    const signal = await parked.promise
    stop.abort()
    // The query ends. How depends on a race: the SDK fails the pending
    // permission request ("Tool permission stream closed before response
    // received"), and Claude Code may reach the model and yield a result
    // before the abort ends the stream (sessions/manager.ts finish).
    const { result, error } = await running
    if (result) expect(JSON.stringify(fake.messageCalls().at(-1)?.body?.messages)).toContain('Tool permission')
    else expect(String(error)).toMatch(/abort/i)
    expect(handled).toEqual([])
    // The SDK aborts the pending permission request's signal on the way down.
    await expect.poll(() => signal.aborted, { timeout: 5_000 }).toBe(true)
  }, 60_000)
})
