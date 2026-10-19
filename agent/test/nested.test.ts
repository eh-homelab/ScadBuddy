import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import { BudgetSpentError, nestedQueries } from '../src/builtins/nested.js'
import { webFetch } from '../src/builtins/web.js'
import { type AttemptOutcome, type CredentialSource, NoUsableCredentialError, type PooledCredential } from '../src/harness/fallback.js'
import type { HarnessRun } from '../src/harness/run.js'

// A durable turn's WebFetch and WebSearch model work (#3200): each query runs on the
// usable credentials in order, as a turn does (runWithFallback), and says why when none is
// usable. Scripted queries stand in for Claude Code, shaped as test/fallback.test.ts's.

const pooled = (id: string, secret: string): PooledCredential => ({ id, epoch: 0, label: `credential ${id}`, credential: { kind: 'anthropic_api_key', secret } })
const A = pooled('a', 'sk-ant-key-aaaa-1111')
const B = pooled('b', 'sk-ant-key-bbbb-2222')

const init = () => ({ type: 'system', subtype: 'init', session_id: '00000000-0000-4000-8000-0000000000aa', model: 'haiku', mcp_servers: [] }) as unknown as SDKMessage
const refused = (): SDKMessage[] => [
  init(),
  { type: 'assistant', message: { model: '<synthetic>', content: [{ type: 'text', text: 'Failed to authenticate. API Error: 403 not allowed' }] }, error: 'authentication_failed' } as unknown as SDKMessage,
  {
    type: 'result',
    subtype: 'success',
    is_error: true,
    api_error_status: 403,
    terminal_reason: 'api_error',
    result: 'Failed to authenticate. API Error: 403 not allowed',
    total_cost_usd: 0,
    num_turns: 1,
  } as unknown as SDKMessage,
]
const answered = (t: string, extra: SDKMessage[] = []): SDKMessage[] => [
  init(),
  ...extra,
  { type: 'result', subtype: 'success', is_error: false, result: t, total_cost_usd: 0.01, num_turns: 1 } as unknown as SDKMessage,
]
const searchResult = (t: string) =>
  ({ type: 'user', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: t }] } }) as unknown as SDKMessage

function harness(
  script: (attempt: number) => SDKMessage[],
  candidates: () => Promise<PooledCredential[]> = async () => [A, B],
  remaining?: () => Promise<number | undefined>,
) {
  const secrets: string[] = []
  /** Each query's maxBudgetUsd. */
  const caps: (number | undefined)[] = []
  const reports: { id: string; outcome: AttemptOutcome['class']; next: string | undefined }[] = []
  const spent: number[] = []
  const credentials: CredentialSource = {
    candidates,
    reporter: () => async (attempt, outcome, next) => void reports.push({ id: attempt.id, outcome: outcome.class, next: next?.id }),
  }
  const nested = nestedQueries({
    paths: { stateDir: '/tmp/unused' },
    credentials,
    spent: async (cost) => void spent.push(cost),
    ...(remaining ? { remaining } : {}),
    run: (r: HarnessRun) => {
      secrets.push(r.credential.secret)
      caps.push(r.maxBudgetUsd)
      const out = script(secrets.length - 1)
      return (async function* () {
        yield* out
      })()
    },
    probe: async () => ({ verdict: 'refused', reason: 'HTTP 403 again' }),
  })
  return { nested, secrets, reports, spent, caps }
}

describe('nested WebFetch and WebSearch queries (#3200)', () => {
  it("falls back to the next credential when the first is refused, as a turn's query does", async () => {
    const h = harness((n) => (n === 0 ? refused() : answered('the answer')))
    expect(await h.nested.ask('What is the title?', new AbortController().signal)).toBe('the answer')
    expect(h.secrets).toEqual([A.credential.secret, B.credential.secret])
    expect(h.reports).toEqual([
      { id: 'a', outcome: 'permanent', next: 'b' },
      { id: 'b', outcome: 'ok', next: undefined },
    ])
    expect(h.spent).toEqual([0.01])
  })

  it('falls back for a search too', async () => {
    const h = harness((n) => (n === 0 ? refused() : answered('done', [searchResult('Web search results for query: "openscad"')])))
    expect(await h.nested.search({ query: 'openscad' }, new AbortController().signal)).toEqual({ text: 'Web search results for query: "openscad"' })
    expect(h.secrets).toEqual([A.credential.secret, B.credential.secret])
  })

  it("spends no more than the session's budget has left, and nothing once it is spent", async () => {
    let left: number | undefined = 0.2
    const h = harness(() => answered('done', [searchResult('results')]), undefined, async () => left)
    expect(await h.nested.ask('p', new AbortController().signal)).toBe('done')
    expect(await h.nested.search({ query: 'openscad' }, new AbortController().signal)).toEqual({ text: 'results' })
    left = 5
    expect(await h.nested.ask('p', new AbortController().signal)).toBe('done')
    // Under the session's 0.2 left, each is capped at it; with more left, at its own cap.
    expect(h.caps).toEqual([0.2, 0.2, 0.5])
    left = 0
    await expect(h.nested.ask('p', new AbortController().signal)).rejects.toBeInstanceOf(BudgetSpentError)
    expect(await h.nested.search({ query: 'openscad' }, new AbortController().signal)).toEqual({
      text: "WebSearch cannot run: the session's budget is spent",
      isError: true,
    })
    expect(h.caps).toHaveLength(3)
    // A session that is gone says so, not that its budget is spent.
    left = undefined
    await expect(h.nested.ask('p', new AbortController().signal)).rejects.toThrow('the session is gone')
    expect(h.caps).toHaveLength(3)
  })

  it('says why when no credential is usable', async () => {
    const none = async (): Promise<PooledCredential[]> => {
      throw new NoUsableCredentialError('every Claude credential is disabled', undefined)
    }
    const h = harness(() => answered('unused'), none)
    await expect(h.nested.ask('p', new AbortController().signal)).rejects.toBeInstanceOf(NoUsableCredentialError)
    expect(await h.nested.search({ query: 'openscad' }, new AbortController().signal)).toEqual({
      text: 'WebSearch cannot run: every Claude credential is disabled',
      isError: true,
    })
    expect(h.secrets).toEqual([])
  })

  it('answers a WebFetch with the cause, as a tool error, when no credential is usable', async () => {
    const pages = createServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/plain' })
      res.end('a page')
    })
    await new Promise<void>((r) => pages.listen(0, '127.0.0.1', r))
    try {
      const none = async (): Promise<PooledCredential[]> => {
        throw new NoUsableCredentialError('every Claude credential is disabled', undefined)
      }
      const h = harness(() => answered('unused'), none)
      const web = {
        nested: h.nested,
        secrets: () => [],
        actor: { kind: 'browser', id: 'browser', label: 'You' },
        signal: new AbortController().signal,
        resolve: async () => ['127.0.0.1'],
        allowLoopback: true,
      }
      const port = (pages.address() as AddressInfo).port
      expect(await webFetch(web, { url: `http://nocredential.test:${port}/`, prompt: 'p' })).toEqual({
        text: 'WebFetch cannot run: every Claude credential is disabled',
        isError: true,
      })
    } finally {
      await new Promise((r) => pages.close(r))
    }
  })
})
