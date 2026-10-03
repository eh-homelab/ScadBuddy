import { randomUUID } from 'node:crypto'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createSdkMcpServer, type SDKMessage, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import {
  type AttemptOutcome,
  CONTINUE_PROMPT,
  DEFAULT_TRANSIENT_RETRIES,
  type PooledCredential,
  runWithFallback,
} from '../src/harness/fallback.js'
import type { HarnessRun } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// #1093 end to end: runWithFallback over the real SDK and its bundled Claude
// Code binary, against one fake Anthropic endpoint that answers each
// credential differently (by its bearer token). Nothing reaches Anthropic.
// These are also the measurements credentialErrors.ts and fallback.ts cite.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const TOKEN_A = 'gw-fallback-first-token-aaaa'
const TOKEN_B = 'gw-fallback-second-token-bbbb'

describe.skipIf(cliMissing !== undefined)(`credential fallback against a fake endpoint${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let forA: (r: RecordedRequest) => Reply
  let forB: (r: RecordedRequest) => Reply
  let stateDir: string
  let handled: string[]

  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'fallback-'))
    await ensureStateDirs({ stateDir })
    handled = []
    fake = await startFakeAnthropic((r) => (r.headers.authorization === `Bearer ${TOKEN_A}` ? forA(r) : forB(r)))
  })
  afterEach(async () => {
    await fake.close()
  })

  const pooled = (id: string, secret: string): PooledCredential => ({
    id,
    epoch: 0,
    label: `credential ${id}`,
    credential: { kind: 'gateway', baseUrl: fake.url, secret },
  })
  const callsWith = (token: string) => fake.messageCalls().filter((c) => c.headers.authorization === `Bearer ${token}`)
  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  async function turn(extra: Partial<HarnessRun> = {}) {
    const reports: { id: string; outcome: AttemptOutcome; next: string | undefined }[] = []
    const messages: SDKMessage[] = []
    let error: unknown
    try {
      for await (const m of runWithFallback(
        { paths: { stateDir }, prompt: 'Make a box', model: 'claude-sonnet-4-5', maxTurns: 4, sessionId: randomUUID(), ...extra },
        {
          candidates: [pooled('a', TOKEN_A), pooled('b', TOKEN_B)],
          report: (attempt, outcome, next) => {
            reports.push({ id: attempt.id, outcome, next: next?.id })
            return Promise.resolve()
          },
        },
      )) {
        messages.push(m)
      }
    } catch (err) {
      error = err
    }
    return { reports, messages, error, result: messages.find((m) => m.type === 'result') }
  }

  it('falls back from a revoked key at its first retry and disables it', async () => {
    forA = () => ({ error: { status: 401, type: 'authentication_error', message: `invalid token ${TOKEN_A}` } })
    forB = () => ({ text: 'Box made.' })
    const { result, reports, messages, error } = await turn()
    expect(error).toBeUndefined()
    expect(result).toMatchObject({ subtype: 'success', is_error: false, result: 'Box made.' })
    // Stopped at the first retry: the SDK then gives Claude Code 2 s to exit
    // before SIGTERM, in which it may make its (bounded) retries, no more.
    expect(callsWith(TOKEN_A).length).toBeLessThanOrEqual(1 + DEFAULT_TRANSIENT_RETRIES)
    expect(reports.map((r) => [r.id, r.outcome.class, r.next])).toEqual([
      ['a', 'permanent', 'b'],
      ['b', 'ok', undefined],
    ])
    // The second credential resumed the session: the model saw the prompt once, then the continuation.
    const texts = JSON.stringify(callsWith(TOKEN_B).at(-1)?.body?.messages)
    expect(texts.split('Make a box').length - 1).toBe(1)
    expect(texts).toContain(CONTINUE_PROMPT)
    expect(JSON.stringify(messages)).not.toContain(TOKEN_A)
    expect(JSON.stringify(messages)).not.toMatch(/Not logged in|API Error/)
  }, 60_000)

  it('cools a rate-limited key down until the time the endpoint names', async () => {
    // A long retry-after: Claude Code does not retry and reports no time, so the
    // time comes from asking the endpoint again (credentialErrors.ts probeRateLimit).
    forA = () => ({
      error: {
        status: 429,
        type: 'rate_limit_error',
        message: 'Number of requests has exceeded your rate limit',
        headers: {
          'retry-after': '600',
          'anthropic-ratelimit-requests-remaining': '0',
          'anthropic-ratelimit-requests-reset': new Date(Date.now() + 600_000).toISOString(),
        },
      },
    })
    forB = () => ({ text: 'Box made.' })
    const started = Date.now()
    const { result, reports } = await turn()
    expect(result).toMatchObject({ subtype: 'success', is_error: false })
    const cooled = reports[0]?.outcome
    expect(cooled).toMatchObject({ class: 'rate_limited' })
    const until = cooled && 'until' in cooled ? cooled.until.getTime() : 0
    expect(until).toBeGreaterThanOrEqual(started + 600_000)
    expect(until).toBeLessThan(Date.now() + 601_000)
    // The probe was one more request with that key, for one token.
    expect(callsWith(TOKEN_A).at(-1)?.body).toMatchObject({ max_tokens: 1, model: 'claude-sonnet-4-5' })
  }, 60_000)

  it('retries an overloaded endpoint a bounded number of times, then falls back for this turn only', async () => {
    forA = () => ({ error: { status: 529, type: 'overloaded_error', message: 'Overloaded' } })
    forB = () => ({ text: 'Box made.' })
    const { result, reports } = await turn()
    expect(result).toMatchObject({ subtype: 'success', is_error: false })
    // The first request and DEFAULT_TRANSIENT_RETRIES (2) retries, all on the first key.
    expect(callsWith(TOKEN_A)).toHaveLength(3)
    expect(reports[0]).toMatchObject({ id: 'a', outcome: { class: 'transient' }, next: 'b' })
  }, 60_000)

  it('does not fall back on a bad request: the next key would fail the same way', async () => {
    forA = () => ({ error: { status: 400, type: 'invalid_request_error', message: 'messages: bad' } })
    forB = () => ({ text: 'never' })
    const { result, error, reports } = await turn()
    expect(result).toMatchObject({ is_error: true, api_error_status: 400 })
    expect(error).toBeInstanceOf(Error)
    expect(callsWith(TOKEN_B)).toHaveLength(0)
    expect(reports.map((r) => r.outcome.class)).toEqual(['ok'])
  }, 60_000)

  it('falls back on a billing refusal and disables the key', async () => {
    forA = () => ({
      error: { status: 400, type: 'invalid_request_error', message: 'Your credit balance is too low to access the Anthropic API.' },
    })
    forB = () => ({ text: 'Box made.' })
    const { result, reports } = await turn()
    expect(result).toMatchObject({ subtype: 'success', is_error: false })
    expect(reports[0]).toMatchObject({ id: 'a', outcome: { class: 'permanent' }, next: 'b' })
  }, 60_000)

  it('resumes a turn that is rate limited after a tool ran, without running the tool again', async () => {
    const echo = tool('echo', 'Echo text back', { text: z.string() }, (args) => {
      handled.push(args.text)
      return Promise.resolve({ content: [{ type: 'text' as const, text: `echo:${args.text}` }] })
    })
    forA = (r) =>
      lastContent(r).includes('tool_result')
        ? { error: { status: 429, type: 'rate_limit_error', message: 'slow down', headers: { 'retry-after': '600' } } }
        : { toolUse: { name: 'mcp__stub__echo', input: { text: 'hi' } } }
    forB = () => ({ text: 'The tool said echo:hi.' })
    const { result, reports, messages } = await turn({
      mcpServers: { stub: createSdkMcpServer({ name: 'stub', tools: [echo] }) },
      tierOf: () => 'read',
    })
    expect(result).toMatchObject({ subtype: 'success', result: 'The tool said echo:hi.' })
    expect(handled).toEqual(['hi'])
    expect(reports.map((r) => [r.id, r.outcome.class])).toEqual([
      ['a', 'rate_limited'],
      ['b', 'ok'],
    ])
    // The caller saw the tool call and its result once, and one init.
    expect(messages.filter((m) => m.type === 'system' && m.subtype === 'init')).toHaveLength(1)
    const sent = callsWith(TOKEN_B).at(-1)?.body?.messages ?? []
    expect(JSON.stringify(sent)).toContain('tool_result')
    expect(JSON.stringify(sent)).toContain(CONTINUE_PROMPT)
    // The cost is what the endpoint billed, once: two answered requests (A's
    // tool call, B's reply), each priced as one plain reply is. The resumed
    // attempt's total already held A's (fallback.ts `Spend`), and the rejected
    // requests (the 429 and the probe) cost nothing.
    forA = () => ({ text: 'One reply.' })
    const reference = await turn({ sessionId: randomUUID() })
    const one = reference.result?.type === 'result' ? reference.result.total_cost_usd : NaN
    expect(one).toBeGreaterThan(0)
    expect(result?.type === 'result' ? result.total_cost_usd : NaN).toBeCloseTo(2 * one, 12)
    expect(result).toMatchObject({ num_turns: 3 })
  }, 60_000)
})
