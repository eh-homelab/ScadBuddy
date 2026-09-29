import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { RiskTier } from '../src/harness/permissions.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { expectPanelAccepts, frontendChatReducer, frontendClientMessages } from './support/frontendProtocol.js'
import { type LiveAgent, openPanelSocket, startLiveAgent } from './support/liveAgent.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, tempPaths } from './support/sessions.js'

// The assistant panel's whole path, end to end: the panel's own client
// messages (frontend protocol.ts `clientMessage`) over a real WebSocket to the
// agent's real HTTP server (createApp on @hono/node-server with `ws`, as
// main.ts runs it), through the chat socket (routes/chat.ts) into the
// SessionManager, the real Agent SDK and its bundled Claude Code binary,
// pointed at the local fake Anthropic endpoint as a gateway; sessions and
// approvals in Postgres. What comes back is parsed with the panel's own schema
// and replayed through the panel's own reducer.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const TOKEN = 'gw-chat-e2e-token-1111222233334444'
const tiers: Record<string, RiskTier> = { mcp__stub__print: 'outward', mcp__stub__echo: 'read' }

type Frame = Record<string, unknown>

describe.skipIf(skip !== undefined)(`the chat socket against the real SDK${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let drop: () => Promise<void>
  let agent: LiveAgent | undefined
  let printed: string[]
  const pools: Database[] = []

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    printed = []
  })
  afterEach(async () => {
    await agent?.close()
    agent = undefined
    await fake.close()
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  function stubServer() {
    const print = tool('print', 'Send to the printer', { job: z.string() }, (args) => {
      printed.push(args.job)
      return Promise.resolve({ content: [{ type: 'text' as const, text: `printing ${args.job}` }] })
    })
    return createSdkMcpServer({ name: 'stub', tools: [print] })
  }

  async function sessions(): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
    pools.push(pool)
    return manager({
      sql: pool.sql,
      paths,
      credential: () => Promise.resolve({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      tierOf: (name) => tiers[name],
      mcpServers: () => ({ stub: stubServer() }),
      approvalPollMs: 50,
      approvalHashKey: Buffer.alloc(32, 7),
    })
  }

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
  const is = (type: string) => (f: Frame) => f.type === type

  it('runs a whole turn from the panel: page context to the model, an outward call parked, approved over the socket, run', async () => {
    script = (r) => {
      const last = lastContent(r)
      if (last.includes('tool_result')) return { text: last.includes('printing') ? 'Sent it to the printer.' : 'Not sent.' }
      return { toolUse: { name: 'mcp__stub__print', input: { job: 'keychain.3mf' } } }
    }
    const m = await sessions()
    agent = await startLiveAgent(m)
    const panel = await openPanelSocket(agent)
    const { clientMessage, parseClientMessage } = await frontendClientMessages()

    // On open: the picker's snapshot (nothing yet).
    expect((await panel.until(is('sessions.snapshot'))).at(-1)).toEqual({ v: 1, type: 'sessions.snapshot', sessions: [] })

    const first = clientMessage({
      type: 'user.message',
      text: 'Print the keychain please',
      context: { route: '/models/name-keychain', modelSlug: 'name-keychain', page: { changed: { text_size: 14 } } },
    })
    expect(parseClientMessage(first).ok).toBe(true)
    panel.send(first)

    const parked = await panel.until(is('approval.required'))
    const started = parked.find(is('session.started'))!
    const sessionId = started.sessionId as string
    expect(started).toMatchObject({ origin: 'chat', owner: browser, title: 'Print the keychain please' })
    // The transcript shows what the user typed; the model also got the page context.
    expect(parked.find(is('user.turn'))).toMatchObject({ text: 'Print the keychain please', author: browser })
    const firstCall = JSON.stringify(fake.messageCalls()[0]?.body?.messages ?? [])
    expect(firstCall).toContain('Print the keychain please')
    expect(firstCall).toContain('<page_context>')
    expect(firstCall).toContain('/models/name-keychain')
    expect(parked.find(is('tool.call'))).toMatchObject({ name: 'mcp__stub__print', risk: 'outward' })
    const required = parked.at(-1)!
    expect(printed).toEqual([])

    // A second tab watching the same session sees it parked too (attach replays).
    const watcher = await openPanelSocket(agent)
    await watcher.until(is('sessions.snapshot'))
    watcher.send(clientMessage({ type: 'session.attach', sessionId }))
    await watcher.until(is('approval.required'))

    const mark = panel.frames.length
    panel.send(clientMessage({ type: 'approval.decision', sessionId, id: required.id as string, approve: true }))
    const rest = await panel.until((f) => f.type === 'session.status' && f.status === 'idle', { from: mark })
    expect(rest.find(is('approval.resolved'))).toMatchObject({ id: required.id, approved: true, by: browser })
    expect(rest.find(is('tool.result'))).toMatchObject({ ok: true })
    expect(rest.find(is('session.result'))).toMatchObject({ turns: expect.any(Number) })
    expect(printed).toEqual(['keychain.3mf'])
    // The watcher got the same ending, live.
    await watcher.until((f) => f.type === 'session.status' && f.status === 'idle', { from: 1 })

    // Everything the panel received is valid for it, and its reducer builds the feed.
    await expectPanelAccepts(panel.frames)
    const { chatReducer, initialChatState } = await frontendChatReducer()
    let state = chatReducer(chatReducer(initialChatState, { type: 'connected' }), { type: 'started-new' })
    for (const frame of panel.frames) state = chatReducer(state, { type: 'server', event: frame })
    expect(state.activeId).toBe(sessionId)
    expect(state.notice).toBeNull()
    const session = state.sessions[sessionId]!
    expect(session.status).toBe('idle')
    expect(session.items.map((i) => i.kind)).toEqual(['user', 'tool', 'approval', 'assistant'])
    expect(session.items.find((i) => i.kind === 'approval')).toMatchObject({ state: 'approved' })
    expect(session.items.find((i) => i.kind === 'assistant')).toMatchObject({ text: 'Sent it to the printer.', done: true })

    // A follow-up turn in the same session, which the panel already follows:
    // no replay, just the new turn.
    script = () => ({ text: 'Anything else?' })
    const before = panel.frames.length
    panel.send(clientMessage({ type: 'user.message', sessionId, text: 'thanks', context: { route: '/' } }))
    const second = await panel.until((f) => f.type === 'session.status' && f.status === 'idle', { from: before })
    expect(second.filter(is('session.started'))).toHaveLength(0)
    expect(second.find(is('user.turn'))).toMatchObject({ text: 'thanks' })
    expect(second.filter(is('assistant.text.delta')).map((f) => f.delta).join('')).toBe('Anything else?')

    // The HTTP view of the same session agrees.
    const res = await fetch(`${agent.url}/api/v1/ai/sessions/${sessionId}`)
    expect(res.headers.get('x-scadbuddy-service')).toBe('agent')
    expect(await res.json()).toMatchObject({ id: sessionId, status: 'idle', origin: 'chat', turns: expect.any(Number) })

    panel.close()
    watcher.close()
  }, 90_000)

  it('keeps the session picker current: a session started elsewhere appears without a reconnect', async () => {
    script = () => ({ text: 'hi' })
    const m = await sessions()
    agent = await startLiveAgent(m, { chatSnapshotMs: 100 })
    const panel = await openPanelSocket(agent)
    await panel.until(is('sessions.snapshot'))
    const quiet = panel.frames.length
    // Unchanged: nothing more is sent.
    await new Promise((r) => setTimeout(r, 400))
    expect(panel.frames.length).toBe(quiet)

    // Another principal starts a session (as an MCP client would); the open panel is told.
    const { session } = await m.start(agentA, { origin: 'mcp', title: 'from Claude Desktop' })
    const seen = await panel.until(
      (f) => f.type === 'sessions.snapshot' && JSON.stringify(f.sessions).includes(session.id),
      { from: quiet, timeoutMs: 5000 },
    )
    expect(seen.at(-1)).toMatchObject({
      sessions: [expect.objectContaining({ sessionId: session.id, title: 'from Claude Desktop', origin: 'mcp', owner: agentA })],
    })
    await expectPanelAccepts(panel.frames)
    panel.close()
  }, 30_000)

  it('interrupts a running turn from the socket', async () => {
    script = () => ({ hang: true })
    const m = await sessions()
    agent = await startLiveAgent(m)
    const panel = await openPanelSocket(agent)
    const { clientMessage } = await frontendClientMessages()
    await panel.until(is('sessions.snapshot'))
    panel.send(clientMessage({ type: 'user.message', text: 'think hard', context: { route: '/' } }))
    const running = await panel.until((f) => f.type === 'session.status' && f.status === 'running')
    const sessionId = running.find(is('session.started'))!.sessionId as string
    // Wait until the model has been asked, so the interrupt stops a real query.
    for (let i = 0; i < 200 && fake.messageCalls().length === 0; i++) await new Promise((r) => setTimeout(r, 50))
    const mark = panel.frames.length
    panel.send(clientMessage({ type: 'session.interrupt', sessionId }))
    const ended = await panel.until((f) => f.type === 'session.status' && f.status !== 'running', { from: mark })
    expect(ended.at(-1)).toMatchObject({ status: 'idle' })
    expect(ended.find(is('error'))).toMatchObject({ code: 'interrupted' })
    await expectPanelAccepts(panel.frames)
    panel.close()
  }, 60_000)

  it('refuses the socket from another origin, and a send while a turn runs', async () => {
    script = () => ({ hang: true })
    const m = await sessions()
    agent = await startLiveAgent(m)
    await expect(openPanelSocket(agent, 'https://evil.example')).rejects.toThrow(/403/)

    const panel = await openPanelSocket(agent)
    const { clientMessage } = await frontendClientMessages()
    await panel.until(is('sessions.snapshot'))
    panel.send(clientMessage({ type: 'user.message', text: 'first', context: { route: '/' } }))
    const running = await panel.until((f) => f.type === 'session.status' && f.status === 'running')
    const sessionId = running.find(is('session.started'))!.sessionId as string
    panel.send(clientMessage({ type: 'user.message', sessionId, text: 'second', context: { route: '/' } }))
    const busy = await panel.until(is('error'))
    expect(busy.at(-1)).toMatchObject({ sessionId, code: 'busy' })
    // Malformed frames are answered, and the socket stays usable.
    panel.send({ v: 1, type: 'nonsense' })
    const mark = panel.frames.length
    await panel.until((f) => f.type === 'error' && f.code === 'invalid', { from: mark - 1 })
    panel.send(clientMessage({ type: 'session.interrupt', sessionId }))
    await panel.until((f) => f.type === 'session.status' && f.status === 'idle')
    panel.close()
  }, 60_000)
})
