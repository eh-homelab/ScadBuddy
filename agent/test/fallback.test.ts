import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import type { Credential } from '../src/credentials.js'
import {
  type AttemptOutcome,
  CONTINUE_PROMPT,
  DEFAULT_TRANSIENT_RETRIES,
  NoUsableCredentialError,
  type PooledCredential,
  runWithFallback,
} from '../src/harness/fallback.js'
import { DEFAULT_COOLDOWN_MS, type FailureEvidence, type ProbeVerdict } from '../src/harness/credentialErrors.js'
import type { HarnessRun } from '../src/harness/run.js'

// runWithFallback with scripted queries standing in for Claude Code (the real
// binary against the fake endpoint is test/fallback.e2e.test.ts). The
// messages are shaped as Claude Code 2.1.283 sends them.

const SESSION = '00000000-0000-4000-8000-0000000000aa'

const pooled = (id: string, secret: string): PooledCredential => ({
  id,
  epoch: 0,
  label: `credential ${id}`,
  credential: { kind: 'anthropic_api_key', secret },
})
const A = pooled('a', 'sk-ant-key-aaaa-1111')
const B = pooled('b', 'sk-ant-key-bbbb-2222')
const C = pooled('c', 'sk-ant-key-cccc-3333')

const init = (model = 'claude-sonnet-4-5') =>
  ({ type: 'system', subtype: 'init', session_id: SESSION, model, mcp_servers: [] }) as unknown as SDKMessage
const retry = (status: number | null, error: string) =>
  ({ type: 'system', subtype: 'api_retry', attempt: 1, max_retries: 10, retry_delay_ms: 500, error_status: status, error }) as unknown as SDKMessage
const text = (t: string) =>
  ({ type: 'assistant', message: { content: [{ type: 'text', text: t }] }, parent_tool_use_id: null }) as unknown as SDKMessage
const toolUse = () =>
  ({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 't1', name: 'mcp__x__y', input: {} }] } }) as unknown as SDKMessage
const toolResult = () =>
  ({ type: 'user', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] } }) as unknown as SDKMessage
const apiError = (t: string, error: string) =>
  ({ type: 'assistant', message: { model: '<synthetic>', content: [{ type: 'text', text: t }] }, error }) as unknown as SDKMessage
const errorResult = (status: number | null, t: string, cost = 0, turns = 1) =>
  ({
    type: 'result',
    subtype: 'success',
    is_error: true,
    api_error_status: status,
    terminal_reason: 'api_error',
    result: t,
    total_cost_usd: cost,
    num_turns: turns,
  }) as unknown as SDKMessage
const success = (t: string, cost = 0.01, turns = 1) =>
  ({ type: 'result', subtype: 'success', is_error: false, result: t, total_cost_usd: cost, num_turns: turns }) as unknown as SDKMessage
const maxTurns = () =>
  ({ type: 'result', subtype: 'error_max_turns', is_error: true, errors: [], total_cost_usd: 0.02, num_turns: 3 }) as unknown as SDKMessage

type Script = (run: HarnessRun, attempt: number) => SDKMessage[] | { messages: SDKMessage[]; throws: Error }

function harness(script: Script) {
  const runs: HarnessRun[] = []
  const reports: { id: string; outcome: AttemptOutcome; next: string | undefined }[] = []
  const probes: (Credential & { model: string | undefined; signal: AbortSignal | undefined })[] = []
  const refusals: { failure: FailureEvidence; requests: number }[] = []
  const run = (r: HarnessRun): AsyncIterable<SDKMessage> => {
    runs.push(r)
    const out = script(r, runs.length - 1)
    return (async function* () {
      const list = Array.isArray(out) ? out : out.messages
      for (const m of list) {
        if (r.signal?.aborted) throw new Error('aborted')
        yield m
      }
      if (!Array.isArray(out)) throw out.throws
    })()
  }
  return {
    runs,
    reports,
    probes,
    refusals,
    async collect(
      candidates: PooledCredential[],
      base: Partial<HarnessRun> = {},
      // By default the probe confirms a refusal: refused again.
      probeVerdict: ProbeVerdict | (() => Promise<ProbeVerdict>) = { verdict: 'refused', reason: 'HTTP 401 again' },
      priorCostUsd?: number,
    ): Promise<{ messages: SDKMessage[]; error: unknown }> {
      const messages: SDKMessage[] = []
      let error: unknown
      try {
        for await (const m of runWithFallback(
          { paths: { stateDir: '/tmp/unused' }, prompt: 'Make a box', sessionId: SESSION, ...base },
          {
            candidates,
            run,
            report: (attempt, outcome, next) => {
              reports.push({ id: attempt.id, outcome, next: next?.id })
              return Promise.resolve()
            },
            probe: (credential, model, signal) => {
              probes.push({ ...credential, model, signal })
              return typeof probeVerdict === 'function' ? probeVerdict() : Promise.resolve(probeVerdict)
            },
            ...(priorCostUsd === undefined ? {} : { priorCostUsd }),
            onRefused: (failure, requests) => {
              refusals.push({ failure, requests })
            },
          },
        )) {
          messages.push(m)
        }
      } catch (err) {
        error = err
      }
      return { messages, error }
    },
  }
}

const kinds = (messages: SDKMessage[]) =>
  messages.map((m) => `${m.type}${'subtype' in m && m.subtype ? `/${m.subtype}` : ''}`)

describe('runWithFallback (#1093)', () => {
  it('runs on the first credential and records that it worked', async () => {
    const h = harness(() => [init(), text('hi'), success('hi')])
    const { messages, error } = await h.collect([A, B])
    expect(error).toBeUndefined()
    expect(kinds(messages)).toEqual(['system/init', 'assistant', 'result/success'])
    expect(h.runs).toHaveLength(1)
    expect(h.runs[0]?.credential).toBe(A.credential)
    // Claude Code's retries are bounded only while there is somewhere to fall back to.
    expect(h.runs[0]?.maxRetries).toBe(DEFAULT_TRANSIENT_RETRIES)
    expect(h.reports).toEqual([{ id: 'a', outcome: { class: 'ok' }, next: undefined }])
  })

  it('leaves Claude Code its own retries on the last credential', async () => {
    const h = harness(() => [init(), success('hi')])
    await h.collect([A])
    expect(h.runs[0]?.maxRetries).toBeUndefined()
  })

  it('throws NoUsableCredentialError with no candidates', async () => {
    const h = harness(() => [])
    expect((await h.collect([])).error).toBeInstanceOf(NoUsableCredentialError)
  })

  it('stops at the first 401 retry, disables the credential and resumes the session on the next', async () => {
    const h = harness((_r, n) =>
      n === 0 ? [init(), retry(401, 'authentication_failed'), retry(401, 'authentication_failed')] : [init(), text('done'), success('done')],
    )
    const { messages, error } = await h.collect([A, B])
    expect(error).toBeUndefined()
    expect(kinds(messages)).toEqual(['system/init', 'assistant', 'result/success'])
    expect(h.runs).toHaveLength(2)
    expect(h.runs[1]).toMatchObject({ credential: B.credential, resume: SESSION, prompt: CONTINUE_PROMPT })
    expect(h.runs[1]?.sessionId).toBeUndefined()
    expect(h.runs[1]?.maxRetries).toBeUndefined()
    // The first attempt was stopped, not waited out.
    expect(h.runs[0]?.signal?.aborted).toBe(true)
    expect(h.reports).toEqual([
      { id: 'a', outcome: { class: 'permanent', reason: 'HTTP 401: authentication_failed' }, next: 'b' },
      { id: 'b', outcome: { class: 'ok' }, next: undefined },
    ])
  })

  it('falls back on a 403 that Claude Code does not retry, hiding its synthetic error and result', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? {
            messages: [init(), apiError('Failed to authenticate. API Error: 403 not allowed', 'authentication_failed'), errorResult(403, 'Failed to authenticate. API Error: 403 not allowed')],
            throws: new Error('Claude Code returned an error result'),
          }
        : [init(), text('fine'), success('fine')],
    )
    const { messages, error } = await h.collect([A, B])
    expect(error).toBeUndefined()
    expect(JSON.stringify(messages)).not.toContain('API Error')
    expect(h.reports[0]).toEqual({
      id: 'a',
      outcome: { class: 'permanent', reason: 'Failed to authenticate. API Error: 403 not allowed' },
      next: 'b',
    })
  })

  it('cools a rate-limited credential down until the time the endpoint names, asked with the query’s model', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init('claude-opus-4-1'), apiError('API Error: Request rejected (429) · slow down', 'rate_limit'), errorResult(429, 'API Error: Request rejected (429) · slow down')], throws: new Error('error result') }
        : [init(), success('ok')],
    )
    const until = new Date('2026-10-03T12:05:00Z')
    await h.collect([A, B], {}, { verdict: 'rate_limited', until })
    expect(h.probes).toEqual([{ ...A.credential, model: 'claude-opus-4-1', signal: undefined }])
    // At once with the default cooldown and the fallback; then the probe's time replaces it.
    const reason = 'API Error: Request rejected (429) · slow down'
    const provisional = (h.reports[0]?.outcome as { until: Date }).until
    expect(h.reports[0]).toEqual({ id: 'a', outcome: { class: 'rate_limited', reason, until: provisional }, next: 'b' })
    expect(h.reports.slice(1)).toHaveLength(2)
    expect(h.reports.slice(1)).toContainEqual({ id: 'b', outcome: { class: 'ok' }, next: undefined })
    expect(h.reports.slice(1)).toContainEqual({
      id: 'a',
      outcome: { class: 'rate_limited', reason, until, replacing: provisional },
      next: undefined,
    })
    expect(provisional.getTime()).toBeGreaterThan(Date.now() + DEFAULT_COOLDOWN_MS - 5000)
  })

  it('starts the next credential without waiting for a slow rate-limit probe, and writes its answer before returning', async () => {
    let answer: (v: ProbeVerdict) => void = () => {}
    const slow = new Promise<ProbeVerdict>((resolve) => (answer = resolve))
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), apiError('API Error: Request rejected (429)', 'rate_limit'), errorResult(429, 'API Error: Request rejected (429)')], throws: new Error('x') }
        : [init(), success('ok')],
    )
    const done = h.collect([A, B], {}, () => slow)
    await new Promise((r) => setTimeout(r, 20))
    // B ran while the probe was still out.
    expect(h.runs).toHaveLength(2)
    let finished = false
    void done.then(() => (finished = true))
    await new Promise((r) => setTimeout(r, 20))
    expect(finished).toBe(false)
    answer({ verdict: 'answered', until: new Date('2026-10-03T12:00:01Z') })
    await done
    expect(h.reports.at(-1)).toMatchObject({ id: 'a', outcome: { class: 'rate_limited', until: new Date('2026-10-03T12:00:01Z') } })
  })

  it('stops at a 429 retry only when there is a credential to fall back to', async () => {
    const twice = harness((_r, n) => (n === 0 ? [init(), retry(429, 'rate_limit'), success('late')] : [init(), success('ok')]))
    await twice.collect([A, B])
    expect(twice.runs).toHaveLength(2)

    const alone = harness(() => [init(), retry(429, 'rate_limit'), text('waited'), success('waited')])
    const { messages } = await alone.collect([A])
    expect(alone.runs).toHaveLength(1)
    expect(kinds(messages)).toEqual(['system/init', 'system/api_retry', 'assistant', 'result/success'])
    expect(alone.reports).toEqual([{ id: 'a', outcome: { class: 'ok' }, next: undefined }])
  })

  it('lets transient failures retry on the same credential, then falls back without marking it', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? {
            messages: [init(), retry(529, 'overloaded'), retry(529, 'overloaded'), apiError('API Error: 529 Overloaded', 'server_error'), errorResult(529, 'API Error: 529 Overloaded')],
            throws: new Error('error result'),
          }
        : [init(), success('ok')],
    )
    const { messages } = await h.collect([A, B])
    // Claude Code's own retries were passed on; the attempt was not stopped at them.
    expect(kinds(messages)).toEqual(['system/init', 'system/api_retry', 'system/api_retry', 'result/success'])
    expect(h.reports[0]).toEqual({ id: 'a', outcome: { class: 'transient', reason: 'API Error: 529 Overloaded' }, next: 'b' })
  })

  it('does not fall back when the failure is not the credential’s: a bad request, or a turn limit', async () => {
    const bad = harness(() => ({
      messages: [init(), apiError('API Error: 400 messages: bad', 'unknown'), errorResult(400, 'API Error: 400 messages: bad')],
      throws: new Error('Claude Code returned an error result: API Error: 400 messages: bad'),
    }))
    const r1 = await bad.collect([A, B])
    expect(bad.runs).toHaveLength(1)
    expect(kinds(r1.messages)).toEqual(['system/init', 'assistant', 'result/success'])
    expect((r1.error as Error).message).toMatch(/400 messages: bad/)
    expect(bad.reports).toEqual([{ id: 'a', outcome: { class: 'ok' }, next: undefined }])

    const limit = harness(() => ({ messages: [init(), maxTurns()], throws: new Error('error result') }))
    const r2 = await limit.collect([A, B])
    expect(limit.runs).toHaveLength(1)
    expect(kinds(r2.messages)).toEqual(['system/init', 'result/error_max_turns'])
  })

  it('resumes a turn that failed mid-way, with what it spent added to the final result', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? {
            messages: [init(), toolUse(), toolResult(), apiError('API Error: Request rejected (429)', 'rate_limit'), errorResult(429, 'API Error: Request rejected (429)', 0.03, 2)],
            throws: new Error('error result'),
          }
        : // Resumed: its total already holds the first attempt's 0.03 (measured, fallback.e2e.test.ts).
          [init(), text('done'), success('done', 0.04, 1)],
    )
    const { messages } = await h.collect([A, B], { maxTurns: 10, maxBudgetUsd: 1 })
    expect(kinds(messages)).toEqual(['system/init', 'assistant', 'user', 'assistant', 'result/success'])
    // The cost is not added again; the turns are, since num_turns is the query's own.
    expect(messages.at(-1)).toMatchObject({ total_cost_usd: 0.04, num_turns: 3 })
    // The budget check is the query's own spend, so the next attempt gets what is left.
    expect(h.runs[1]).toMatchObject({ resume: SESSION, prompt: CONTINUE_PROMPT, maxTurns: 8, maxBudgetUsd: 0.97 })
  })

  it('counts what the session spent before this turn once, on a turn that itself resumed', async () => {
    // The session had spent 0.5 before; attempt A (resumed) reports 0.5 + 0.03, B 0.5 + 0.03 + 0.01.
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), toolUse(), toolResult(), apiError('API Error: 529', 'server_error'), errorResult(529, 'API Error: 529', 0.53, 2)], throws: new Error('x') }
        : [init(), success('done', 0.54, 1)],
    )
    const { messages } = await h.collect([A, B], { resume: SESSION, sessionId: undefined, maxBudgetUsd: 0.5 }, undefined, 0.5)
    expect(messages.at(-1)).toMatchObject({ total_cost_usd: 0.54, num_turns: 3 })
    expect(h.runs[1]?.maxBudgetUsd).toBeCloseTo(0.47, 10)
  })

  it('leaves the last credential’s failed result as it came: its cost and turns count themselves once', async () => {
    const h = harness(() => ({
      messages: [init(), toolUse(), toolResult(), apiError('API Error: 529 Overloaded', 'server_error'), errorResult(529, 'API Error: 529 Overloaded', 0.03, 2)],
      throws: new Error('Claude Code returned an error result'),
    }))
    const { messages, error } = await h.collect([A])
    expect(messages.at(-1)).toMatchObject({ type: 'result', total_cost_usd: 0.03, num_turns: 2 })
    expect(error).toBeInstanceOf(Error)
    expect(h.reports).toEqual([{ id: 'a', outcome: { class: 'transient', reason: 'API Error: 529 Overloaded' }, next: undefined }])
  })

  it('adds only an earlier attempt’s turns to the last credential’s failed result after a fallback', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), toolUse(), toolResult(), apiError('API Error: 529', 'server_error'), errorResult(529, 'API Error: 529', 0.03, 2)], throws: new Error('x') }
        : { messages: [init(), toolUse(), toolResult(), apiError('API Error: 529', 'server_error'), errorResult(529, 'API Error: 529', 0.05, 1)], throws: new Error('x') },
    )
    const { messages } = await h.collect([A, B])
    // B resumed: its 0.05 already holds A's 0.03.
    expect(messages.at(-1)).toMatchObject({ total_cost_usd: 0.05, num_turns: 3 })
  })

  it('says what the turn ended on, and how many refused requests its turns count (#1101)', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), toolUse(), toolResult(), retry(529, 'server_error'), apiError('API Error: 429 slow down', 'rate_limit'), errorResult(429, 'API Error: 429 slow down', 0.03, 2)], throws: new Error('x') }
        : { messages: [init(), apiError('API Error: 400 bad', 'invalid_request'), errorResult(null, 'API Error: 400 bad', 0.03, 1)], throws: new Error('x') },
    )
    const { messages } = await h.collect([A, B], {}, { verdict: 'unknown', until: new Date(Date.now() + 60_000) })
    // B's own evidence: neither A's 529 retry nor its rate_limit category.
    expect(h.refusals).toEqual([{ failure: { status: null, category: 'invalid_request', message: 'API Error: 400 bad' }, requests: 2 }])
    // A's tool call (1 turn) and the two refused requests.
    expect(messages.at(-1)).toMatchObject({ type: 'result', num_turns: 3 })
    expect(kinds(messages).slice(-2)).toEqual(['assistant', 'result/success'])
  })

  it('reports no refusal when the turn ends on a reply, or on a synthetic message it went on from (#1101)', async () => {
    const h = harness(() => [init(), apiError('API Error: 529', 'server_error'), text('Carried on.'), success('Carried on.')])
    const { messages } = await h.collect([A])
    expect(kinds(messages)).toEqual(['system/init', 'assistant', 'assistant', 'result/success'])
    expect(h.refusals).toEqual([])
  })

  it('disables a rate-limited credential the probe finds refused outright', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), apiError('API Error: Request rejected (429)', 'rate_limit'), errorResult(429, 'API Error: Request rejected (429)')], throws: new Error('x') }
        : [init(), success('ok')],
    )
    await h.collect([A, B], {}, { verdict: 'refused', reason: `the probe was refused (HTTP 401): bad ${A.credential.secret}` })
    expect(h.reports).toContainEqual({
      id: 'a',
      outcome: { class: 'permanent', reason: 'the probe was refused (HTTP 401): bad [redacted]' },
      next: undefined,
    })
  })

  it('does not disable on a refusal the probe does not confirm: the turn ends there, with no fallback', async () => {
    // Every credential gets the same 403 (a gateway refusing this one request); the key itself works.
    const h = harness(() => ({
      messages: [init(), apiError('Failed to authenticate. API Error: 403 blocked by policy', 'authentication_failed'), errorResult(403, 'Failed to authenticate. API Error: 403 blocked by policy')],
      throws: new Error('Claude Code returned an error result'),
    }))
    const { messages, error } = await h.collect([A, B, C], { model: 'claude-opus-4-1' }, { verdict: 'answered', until: new Date() })
    expect(h.runs).toHaveLength(1)
    // Asked on the probe's own model, not the turn's: a refusal of the turn's model does not confirm itself.
    expect(h.probes.map((p) => p.model)).toEqual([undefined])
    expect(h.reports).toEqual([])
    expect(kinds(messages)).toEqual(['system/init', 'assistant', 'result/success'])
    expect((error as Error).message).toMatch(/Claude Code returned an error result/)
  })

  it('falls back without disabling when the probe gives no clear answer', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), apiError('API Error: 403', 'authentication_failed'), errorResult(403, 'API Error: 403')], throws: new Error('x') }
        : [init(), success('ok')],
    )
    await h.collect([A, B], {}, { verdict: 'unknown', until: new Date() })
    expect(h.reports[0]).toEqual({ id: 'a', outcome: { class: 'transient', reason: 'API Error: 403' }, next: 'b' })
  })

  it('does not wait for a pending probe once the caller stops the turn', async () => {
    const stop = new AbortController()
    const h = harness((_r, n) => {
      if (n === 0) {
        return { messages: [init(), apiError('API Error: Request rejected (429)', 'rate_limit'), errorResult(429, 'API Error: Request rejected (429)')], throws: new Error('x') }
      }
      stop.abort()
      return { messages: [init()], throws: new Error('aborted') }
    })
    // A probe that only ends when its signal does.
    const never = () => new Promise<ProbeVerdict>(() => {})
    const started = Date.now()
    const { error } = await h.collect([A, B], { signal: stop.signal }, never)
    expect(Date.now() - started).toBeLessThan(1000)
    expect(error).toBeInstanceOf(Error)
    expect(h.probes[0]?.signal).toBe(stop.signal)
  })

  it('keeps the real error, not a "refused", when the allowance ends the chain after a stop at a retry', async () => {
    const rate = harness(() => [init(), retry(429, 'rate_limit')])
    const r1 = await rate.collect([A, B], { maxTurns: 0 })
    expect(rate.runs).toHaveLength(1)
    expect((r1.error as Error).message).toBe(
      "the Claude credential (credential a) is rate limited: HTTP 429: rate_limit; no other credential was tried, since the turn's turns or budget are used up",
    )
    // A refusal the probe could not confirm is not reported as "refused".
    const h = harness(() => [init(), retry(401, 'authentication_failed')])
    const r2 = await h.collect([A], {}, { verdict: 'unknown', until: new Date() })
    expect((r2.error as Error).message).toBe('the Claude credential (credential a) failed: HTTP 401: authentication_failed')
  })

  it('starts no further attempt once the turn’s allowance is used up, showing the failure as it came', async () => {
    const failing = (cost: number, turns: number) => ({
      messages: [init(), toolUse(), toolResult(), apiError('API Error: 529', 'server_error'), errorResult(529, 'API Error: 529', cost, turns)],
      throws: new Error('x'),
    })
    const byTurns = harness(() => failing(0.01, 3))
    const r1 = await byTurns.collect([A, B], { maxTurns: 3 })
    expect(byTurns.runs).toHaveLength(1)
    expect(r1.messages.at(-1)).toMatchObject({ type: 'result', num_turns: 3, total_cost_usd: 0.01 })
    expect(byTurns.reports).toEqual([{ id: 'a', outcome: { class: 'transient', reason: 'API Error: 529' }, next: undefined }])

    const byBudget = harness(() => failing(0.5, 1))
    await byBudget.collect([A, B], { maxBudgetUsd: 0.5 })
    expect(byBudget.runs).toHaveLength(1)
  })

  it('does not stop a turn that has made progress at a retry: its result says what it spent', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), toolUse(), toolResult(), retry(401, 'authentication_failed'), apiError('Not logged in', 'authentication_failed'), errorResult(401, 'Not logged in', 0.02, 1)], throws: new Error('x') }
        : [init(), success('ok', 0.01)],
    )
    await h.collect([A, B])
    expect(h.runs[0]?.signal?.aborted).toBe(true)
    expect(h.reports[0]?.outcome).toEqual({ class: 'permanent', reason: 'Not logged in' })
    expect(h.runs).toHaveLength(2)
  })

  it('tries each credential once, and surfaces the last failure as it came', async () => {
    const h = harness(() => ({
      messages: [init(), apiError('Failed to authenticate. API Error: 403', 'authentication_failed'), errorResult(403, 'Failed to authenticate. API Error: 403')],
      throws: new Error('Claude Code returned an error result: Failed to authenticate'),
    }))
    const { messages, error } = await h.collect([A, B, C])
    expect(h.runs.map((r) => r.credential)).toEqual([A.credential, B.credential, C.credential])
    expect(kinds(messages)).toEqual(['system/init', 'assistant', 'result/success'])
    expect((error as Error).message).toMatch(/Failed to authenticate/)
    expect(h.reports.map((r) => [r.id, r.outcome.class, r.next])).toEqual([
      ['a', 'permanent', 'b'],
      ['b', 'permanent', 'c'],
      ['c', 'permanent', undefined],
    ])
  })

  it('says which credential was refused when the last one is stopped at a retry', async () => {
    const h = harness(() => [init(), retry(401, 'authentication_failed')])
    const { error } = await h.collect([A])
    expect((error as Error).message).toBe('the Claude credential (credential a) was refused: HTTP 401: authentication_failed')
  })

  it('redacts the credential from the recorded reason', async () => {
    const h = harness((_r, n) =>
      n === 0
        ? { messages: [init(), apiError(`invalid token ${A.credential.secret}`, 'authentication_failed'), errorResult(401, 'x')], throws: new Error('x') }
        : [init(), success('ok')],
    )
    await h.collect([A, B])
    expect(JSON.stringify(h.reports)).not.toContain(A.credential.secret)
    expect(h.reports[0]?.outcome).toMatchObject({ reason: 'invalid token [redacted]' })
  })

  it('runs a query again as it was when Claude Code never reported a session', async () => {
    const h = harness((_r, n) => (n === 0 ? [retry(401, 'authentication_failed')] : [init(), success('ok')]))
    await h.collect([A, B])
    expect(h.runs[1]).toMatchObject({ prompt: 'Make a box', sessionId: SESSION })
    expect(h.runs[1]?.resume).toBeUndefined()
  })

  it('stops everything when the caller aborts, and never falls back', async () => {
    const stop = new AbortController()
    const h = harness(() => {
      stop.abort()
      return { messages: [init(), apiError('API Error: 529', 'server_error'), errorResult(529, 'API Error: 529')], throws: new Error('aborted') }
    })
    const { error } = await h.collect([A, B], { signal: stop.signal })
    expect(h.runs).toHaveLength(1)
    expect(error).toBeInstanceOf(Error)
    expect(h.reports).toEqual([])
  })
})
