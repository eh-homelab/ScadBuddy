import { writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { query } from '@anthropic-ai/claude-agent-sdk'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { nestedQueries } from '../src/builtins/nested.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { BUILTINS } from '../src/builtins/server.js'
import SCHEMAS from '../src/builtins/schemas.json' with { type: 'json' }
import { type FakeAnthropic, type RecordedRequest, startFakeAnthropic } from './support/fakeAnthropic.js'
import { tempPaths } from './support/sessions.js'

// #2393 against the bundled Claude Code: the `builtin` turn tools take its built-ins' own
// input schemas (src/builtins/schemas.json; UPDATE_BUILTIN_SCHEMAS=1 writes them again
// after a bump), and WebFetch's and WebSearch's model work runs on it as one-shot queries.

const FILE = fileURLToPath(new URL('../src/builtins/schemas.json', import.meta.url))
const TOKEN = 'gw-builtins-token-0000111122223333'

describe('the built-ins a durable turn provides, on the bundled Claude Code', () => {
  let fake: FakeAnthropic
  let reply: (r: RecordedRequest) => ReturnType<Parameters<typeof startFakeAnthropic>[0]>
  beforeAll(async () => {
    fake = await startFakeAnthropic((r) => reply(r))
  })
  afterAll(async () => {
    await fake.close()
  })
  const paths = async () => {
    const p = await tempPaths()
    await ensureStateDirs(p)
    return p
  }
  const credential = () => Promise.resolve({ kind: 'gateway' as const, baseUrl: fake.url, secret: TOKEN })

  it("takes Claude Code's own input schemas", async () => {
    reply = () => ({ text: 'ok' })
    const names = Object.keys(BUILTINS)
    const from = fake.messageCalls().length
    for await (const _ of query({
      prompt: 'go',
      options: { tools: names, settingSources: [], cwd: '/tmp', maxTurns: 1, env: { ...process.env, ANTHROPIC_BASE_URL: fake.url, ANTHROPIC_AUTH_TOKEN: TOKEN, ANTHROPIC_API_KEY: '' } },
    }))
      void _
    const sent = fake.messageCalls().slice(from).find((c) => c.body?.tools?.length)!.body!.tools!
    const theirs = Object.fromEntries(
      names.map((name) => {
        const { $schema: _, ...schema } = (sent.find((t) => t.name === name) as unknown as { input_schema: Record<string, unknown> }).input_schema
        return [name, schema]
      }),
    )
    if (process.env.UPDATE_BUILTIN_SCHEMAS) writeFileSync(FILE, `${JSON.stringify(theirs, null, 2)}\n`)
    expect(SCHEMAS).toEqual(theirs)
  }, 120_000)

  it("asks the small model with no tools, and counts what it cost", async () => {
    reply = () => ({ text: 'the title is Title' })
    const spent: number[] = []
    const nested = nestedQueries({ paths: await paths(), credential, spent: async (c) => void spent.push(c) })
    const from = fake.messageCalls().length
    expect(await nested.ask('What is the title?', new AbortController().signal)).toBe('the title is Title')
    const calls = fake.messageCalls().slice(from)
    expect(calls.at(-1)?.body?.tools ?? []).toEqual([])
    expect(spent.length).toBe(1)
    expect(spent[0]).toBeGreaterThan(0)
  }, 120_000)

  it("runs Claude Code's own WebSearch for exactly the search asked, and returns its result", async () => {
    let step = 0
    reply = (r) => {
      // The search's own model call carries the API's web search tool; the query's offers WebSearch.
      if (r.body?.tools?.some((t) => t.name === 'WebSearch')) {
        return step++ === 0 ? { toolUse: { name: 'WebSearch', input: { query: 'openscad' } } } : { text: 'done' }
      }
      return { text: 'Found OpenSCAD.' }
    }
    const nested = nestedQueries({ paths: await paths(), credential })
    const result = await nested.search({ query: 'openscad' }, new AbortController().signal)
    expect(result.isError).toBeUndefined()
    expect(result.text).toContain('Web search results for query: "openscad"')
    // A search the query's model changed is refused, never run.
    step = 0
    reply = (r) => {
      if (r.body?.tools?.some((t) => t.name === 'WebSearch')) {
        return step++ === 0 ? { toolUse: { name: 'WebSearch', input: { query: 'something else' } } } : { text: 'done' }
      }
      return { text: 'should not run' }
    }
    const refused = await nested.search({ query: 'openscad' }, new AbortController().signal)
    expect(refused.isError).toBe(true)
    expect(refused.text).not.toContain('Web search results')
  }, 180_000)
})
