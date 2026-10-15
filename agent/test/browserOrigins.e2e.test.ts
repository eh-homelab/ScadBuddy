import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { browserGuard, type BrowserOriginsAccess } from '../src/browser/guard.js'
import type { ApprovedScope, BrowserLists } from '../src/browser/origins.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { ApprovalRequest } from '../src/harness/permissions.js'
import { runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { forwardForRun, PluginForwarder, type PluginsForRun } from '../src/plugins/forwarder.js'
import { type FakeAnthropic, type RecordedRequest, startFakeAnthropic } from './support/fakeAnthropic.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'

// #982 end to end on a classic turn: the real SDK and its bundled Claude Code, a fake
// Anthropic endpoint, and a fake Playwright MCP behind the plugin forwarder. The
// browser's navigation lists decide each browser_navigate before it runs (the
// permission seam's input guard), and a page it reached that the lists do not let the
// model read is replaced in its result (a PostToolUse hook's `updatedMCPToolOutput`,
// measured here on the bundled Claude Code). Nothing leaves the machine.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const SESSION = '00000000-0000-4000-8000-0000000009a2'
const LISTS: BrowserLists = { allow: ['makerworld.com'], deny: ['evil.example', '169.254.0.0/16'] }

function memoryStore(): BrowserOriginsAccess & { rows: Map<string, ApprovedScope> } {
  const rows = new Map<string, ApprovedScope>()
  return {
    rows,
    lists: () => Promise.resolve(LISTS),
    approved: () => Promise.resolve(new Map(rows)),
    approve: (_id, origin, scope) => {
      if (rows.get(origin) !== 'session') rows.set(origin, scope)
      return Promise.resolve(true)
    },
  }
}

// Each test spawns Claude Code, two for the redirect test: more than the default 20 s under load.
describe.skipIf(cliMissing !== undefined)(`the headless browser's lists on a classic turn${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, { timeout: 90_000 }, () => {
  let forwarder: PluginForwarder
  let fake: FakeAnthropic
  let mcp: FakeMcp
  let stateDir: string
  let forwarded: PluginsForRun | undefined
  let navigateTo: string
  let store: ReturnType<typeof memoryStore>

  beforeAll(async () => {
    forwarder = await PluginForwarder.start()
  })
  afterAll(async () => {
    await forwarder.close()
  })
  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'browser-e2e-'))
    await ensureStateDirs({ stateDir })
    store = memoryStore()
    const last = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
    fake = await startFakeAnthropic((r) =>
      last(r).includes('tool_result')
        ? { text: 'done' }
        : { toolUse: { name: 'mcp__playwright__browser_navigate', input: { url: navigateTo } } },
    )
    mcp = await startFakeMcp({
      browser: {
        redirects: {
          'https://makerworld.com/go': 'https://elsewhere.example/landing',
          'https://makerworld.com/bad': 'https://evil.example/',
          'https://makerworld.com/slow': 'https://evil.example/slow',
        },
        fails: ['https://makerworld.com/slow'],
      },
    })
  })
  afterEach(async () => {
    forwarded?.release()
    await fake.close()
    await mcp.close()
  })

  /** One turn that navigates to `url`; the gate approves whatever it is asked. */
  async function navigate(url: string): Promise<{ asked: ApprovalRequest[]; seen: string; messages: SDKMessage[] }> {
    navigateTo = url
    forwarded = forwardForRun(
      { plugins: [{ plugin: { name: 'playwright', url: mcp.url, toolTiers: {}, disabledTools: [] }, address: '127.0.0.1' }], problems: [] },
      forwarder,
    )
    const asked: ApprovalRequest[] = []
    const messages: SDKMessage[] = []
    for await (const m of runHarness({
      paths: { stateDir },
      credential: { kind: 'gateway', baseUrl: fake.url, secret: 'gw-browser-e2e-token-000011112222' },
      prompt: 'open it',
      model: 'claude-sonnet-4-5',
      remotePlugins: forwarded.plugins,
      browser: browserGuard({ store, sessionId: SESSION, plugins: ['playwright'] }),
      approvalGate: (request) => {
        asked.push(request)
        return Promise.resolve({ approved: true, input: request.input })
      },
    }))
      messages.push(m)
    const seen = JSON.stringify(fake.messageCalls().at(-1)?.body?.messages?.at(-1)?.content ?? '')
    return { asked, seen, messages }
  }

  it('an allowed origin opens without asking', async () => {
    const { asked, seen } = await navigate('https://makerworld.com/en/models/171292')
    expect(asked).toEqual([])
    expect(mcp.calls).toEqual(['browser_navigate:https://makerworld.com/en/models/171292'])
    expect(seen).toContain('The text of https://makerworld.com/en/models/171292')
  })

  it('a denied origin is refused even though the gate would approve it, naming the rule', async () => {
    const { asked, seen } = await navigate('https://evil.example/login')
    expect(asked).toEqual([])
    expect(mcp.calls).toEqual([])
    expect(seen).toContain('deny rule \\"evil.example\\"')
  })

  it('an unlisted origin asks first, opens once approved, and its pages may then be read', async () => {
    const { asked, seen } = await navigate('https://example.org/model')
    expect(asked.map((a) => [a.toolName, a.tier, a.input])).toEqual([
      ['mcp__playwright__browser_navigate', 'outward', { url: 'https://example.org/model' }],
    ])
    expect(mcp.calls).toEqual(['browser_navigate:https://example.org/model'])
    expect(seen).toContain('The text of https://example.org/model')
    expect(store.rows.get('https://example.org')).toBe('once')
  })

  it('a lookalike host is not the allowed one: it asks', async () => {
    const { asked } = await navigate('https://makerworld.com.evil2.example/')
    expect(asked).toHaveLength(1)
  })

  it('a redirect from an allowed page to an unlisted or denied one is withheld from the model and the transcript', async () => {
    for (const [from, to, why] of [
      ['https://makerworld.com/go', 'https://elsewhere.example', /not on the allow list/],
      ['https://makerworld.com/bad', 'https://evil.example', /deny rule/],
    ] as const) {
      const { asked, seen, messages } = await navigate(from)
      expect(asked).toEqual([])
      expect(seen).toContain(`withheld this result: the browser is now on ${to}`)
      expect(seen).toMatch(why)
      expect(seen).not.toContain('The text of')
      // What the panel shows is the replaced result too.
      expect(JSON.stringify(messages.filter((m) => m.type === 'user'))).not.toContain('The text of')
      forwarded?.release()
    }
  })

  it('an error result from a page the lists do not let it read is withheld too', async () => {
    const { seen, messages } = await navigate('https://makerworld.com/slow')
    expect(seen).toContain('withheld this result: the browser is now on https://evil.example')
    expect(seen).not.toContain('The text of')
    expect(JSON.stringify(messages.filter((m) => m.type === 'user'))).not.toContain('The text of')
  })
})
