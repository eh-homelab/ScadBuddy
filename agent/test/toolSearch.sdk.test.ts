import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { createBackendClient } from '../src/api/backend.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { TOOL_SEARCH, type ToolSearchMode, UPFRONT_TOOLS } from '../src/harness/toolSearch.js'
import { systemAppend } from '../src/sessions/turnShared.js'
import { ALL_TOOLS, tierOf } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { createHarnessServer, SERVER_NAME } from '../src/tools/projections.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// Tool search (#5221) on the real SDK and its bundled Claude Code, through the credential
// gateway (gateway/server.ts) to the local fake endpoint, with the whole tool registry
// in-process: what the first request carries in each mode, and a deferred tool found by
// ToolSearch and then called.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const TOKEN = 'gw-tool-search-token-0000111122223333'
const BACKEND = 'http://backend.tool-search.test'
const FONTS = `mcp__${SERVER_NAME}__list_fonts`
/** Measured on Claude Code 2.1.310 (harness/toolSearch.ts): 334,793 bytes with every tool upfront, 42,516 deferred. */
const DEFERRED_MAX_BYTES = 80_000

type Tool = { name: string; defer_loading?: boolean }
const toolsOf = (r: RecordedRequest) => (r.body?.tools ?? []) as Tool[]
const bytesOf = (r: RecordedRequest) => JSON.stringify(r.body).length
const betas = (r: RecordedRequest) => String(r.headers['anthropic-beta'] ?? '').split(',')

describe.skipIf(cliMissing !== undefined)(`tool search on the real SDK${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest, n: number) => Reply
  let backendCalls: string[]
  let paths: { stateDir: string }

  beforeEach(async () => {
    let n = 0
    fake = await startFakeAnthropic((r) => script(r, n++))
    backendCalls = []
    paths = { stateDir: await mkdtemp(path.join(os.tmpdir(), 'tool-search-')) }
    await ensureStateDirs(paths)
  })
  afterEach(async () => {
    await fake.close()
  })

  function run(toolSearch: ToolSearchMode | undefined): HarnessRun {
    const services = {
      backend: createBackendClient(BACKEND, (request) => {
        backendCalls.push(new URL(request instanceof Request ? request.url : String(request)).pathname)
        return Promise.resolve(Response.json([{ family: 'Liberation Sans', styles: ['Regular'] }]))
      }),
      pending: new PendingActionStore(),
      pollIntervalMs: 5,
      renderWaitMs: 1000,
    }
    return {
      paths,
      credential: { kind: 'gateway', baseUrl: fake.url, secret: TOKEN },
      prompt: 'Which fonts are installed?',
      model: 'claude-sonnet-4-5',
      tierOf,
      mcpServers: { [SERVER_NAME]: createHarnessServer(ALL_TOOLS, services, { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') }) },
      systemPromptAppend: systemAppend(),
      ...(toolSearch ? { toolSearch } : {}),
    }
  }

  async function drain(r: HarnessRun): Promise<SDKMessage[]> {
    const out: SDKMessage[] = []
    for await (const m of runHarness(r)) out.push(m)
    return out
  }

  it('auto defers every tool but the upfront set, and a deferred one is found by ToolSearch and called', async () => {
    script = (_r, n) =>
      n === 0
        ? { toolUse: { name: TOOL_SEARCH, input: { query: `select:${FONTS}`, max_results: 5 } } }
        : n === 1
          ? { toolUse: { name: FONTS, input: {} } }
          : { text: 'Liberation Sans.' }
    const messages = await drain(run('auto'))
    expect(messages.at(-1)).toMatchObject({ type: 'result', subtype: 'success' })
    const [first, second, third] = fake.messageCalls()
    // The first request: ToolSearch and the upfront tools in full, the rest by name only.
    const sent = toolsOf(first!).filter((t) => !t.defer_loading).map((t) => t.name).sort()
    expect(sent).toEqual([TOOL_SEARCH, ...[...UPFRONT_TOOLS].map((n) => `mcp__${SERVER_NAME}__${n}`)].sort())
    expect(toolsOf(first!).some((t) => t.name === FONTS)).toBe(false)
    expect(betas(first!)).toContain('advanced-tool-use-2025-11-20')
    expect(JSON.stringify(first!.body?.messages)).toContain(FONTS)
    expect(bytesOf(first!)).toBeLessThan(DEFERRED_MAX_BYTES)
    // ToolSearch ran (its tier is `read`), its result a tool_reference, and the tool loaded deferred.
    expect(JSON.stringify(second!.body?.messages?.at(-1))).toContain(`"tool_reference","tool_name":"${FONTS}"`)
    expect(toolsOf(second!).find((t) => t.name === FONTS)).toMatchObject({ defer_loading: true })
    // Then it was called, and reached the backend.
    expect(backendCalls).toEqual(['/api/v1/fonts'])
    expect(JSON.stringify(third!.body?.messages?.at(-1))).toContain('Liberation Sans')
  })

  it('off loads every tool upfront and offers no ToolSearch', async () => {
    script = () => ({ text: 'ok' })
    await drain(run('off'))
    const [first] = fake.messageCalls()
    const tools = toolsOf(first!)
    expect(tools.some((t) => t.name === TOOL_SEARCH)).toBe(false)
    expect(tools.some((t) => t.defer_loading)).toBe(false)
    expect(tools.filter((t) => t.name.startsWith(`mcp__${SERVER_NAME}__`))).toHaveLength(ALL_TOOLS.length)
    expect(bytesOf(first!)).toBeGreaterThan(DEFERRED_MAX_BYTES)
  })

  it('always defers too', async () => {
    script = () => ({ text: 'ok' })
    await drain(run('always'))
    const [first] = fake.messageCalls()
    expect(toolsOf(first!).some((t) => t.name === FONTS)).toBe(false)
    expect(bytesOf(first!)).toBeLessThan(DEFERRED_MAX_BYTES)
  })
})
