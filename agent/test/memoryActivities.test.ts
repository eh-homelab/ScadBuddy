import { MockActivityEnvironment } from '@temporalio/testing'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AuditEntry } from '../src/audit/log.js'
import type { PluginsForRun } from '../src/plugins/forwarder.js'
import type { LoggedEvent } from '../src/sessions/eventLog.js'
import { event, type Owner, type ServerEvent } from '../src/sessions/protocol.js'
import {
  MEMORY_RECALL_ACTIVITY,
  MEMORY_RETAIN_ACTIVITY,
  type MemoryActivityDeps,
  memoryActivities,
  type MemoryRecalled,
  transcriptOf,
} from '../src/temporal/memoryActivities.js'
import { type FakeHindsight, startFakeHindsight } from './support/fakeHindsight.js'

// Automatic Hindsight memory in a durable session (#2320): the classic hooks
// (memory/hindsight.ts) run by two activities around the turn, against a fake Hindsight.

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const TURN = '7c2f8d1a-3b4e-4c5d-8e9f-0a1b2c3d4e5f'
const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const TOKEN = 'hs-test-token-0123456789abcdef'
const CREDENTIAL = 'sk-ant-api03-durable-memory-0000'
const BANK = 'scadbuddy'

const log = (...events: ServerEvent[]): LoggedEvent[] => events.map((e, i) => ({ seq: i + 1, event: e }))

/** An event log over `events`: EventLog's reads, as the activities use them. */
function readsOf(events: LoggedEvent[]): Pick<MemoryActivityDeps['events'], 'read' | 'userTurnText'> {
  return {
    read: async (_id, after = 0) => events.filter((e) => e.seq > after),
    userTurnText: async (_id, turnId) => {
      const turn = events.map((e) => e.event).findLast((e) => e.type === 'user.turn' && e.turnId === turnId)
      return turn?.type === 'user.turn' ? turn.text : undefined
    },
  }
}

const conversation = log(
  event({ type: 'user.turn', sessionId: SESSION, turnId: 'earlier', text: 'make a box', author: BROWSER }),
  event({ type: 'assistant.text.delta', sessionId: SESSION, messageId: 'm1', delta: 'Rendering ' }),
  event({ type: 'assistant.text.delta', sessionId: SESSION, messageId: 'm1', delta: 'it now.' }),
  event({ type: 'tool.call', sessionId: SESSION, id: 't1', name: 'render_model', input: { id: 'box' }, risk: 'write' }),
  event({ type: 'tool.call', sessionId: SESSION, id: 't2', name: 'grep', input: { pattern: 'x' }, risk: 'read', parent: 't1' }),
  event({ type: 'user.turn', sessionId: SESSION, turnId: TURN, text: `what colour was it? ${CREDENTIAL}`, author: BROWSER }),
)

describe('transcriptOf', () => {
  it("rebuilds the session's turns from its event log, leaving out a subagent's calls", () => {
    expect(transcriptOf(conversation)).toEqual([
      { type: 'user', message: { content: 'make a box' } },
      { type: 'assistant', message: { content: [{ type: 'text', text: 'Rendering it now.' }] } },
      { type: 'assistant', message: { content: [{ type: 'tool_use', name: 'render_model', input: { id: 'box' } }] } },
      { type: 'user', message: { content: `what colour was it? ${CREDENTIAL}` } },
    ])
  })

  it('puts text that follows a tool call after the call, in one message (#2419)', () => {
    const turn = log(
      event({ type: 'assistant.text.delta', sessionId: SESSION, messageId: 'm1', delta: 'Rendering.' }),
      event({ type: 'tool.call', sessionId: SESSION, id: 't1', name: 'render_model', input: { id: 'box' }, risk: 'write' }),
      event({ type: 'assistant.text.delta', sessionId: SESSION, messageId: 'm1', delta: 'Done.' }),
    )
    expect(transcriptOf(turn)).toEqual([
      { type: 'assistant', message: { content: [{ type: 'text', text: 'Rendering.' }] } },
      { type: 'assistant', message: { content: [{ type: 'tool_use', name: 'render_model', input: { id: 'box' } }] } },
      { type: 'assistant', message: { content: [{ type: 'text', text: 'Done.' }] } },
    ])
  })

  it("keeps a message on one line across a subagent's call between its deltas (#3089)", () => {
    const turn = log(
      event({ type: 'assistant.text.delta', sessionId: SESSION, messageId: 'm1', delta: 'Looking ' }),
      event({ type: 'tool.call', sessionId: SESSION, id: 't2', name: 'grep', input: { pattern: 'x' }, risk: 'read', parent: 't1' }),
      event({ type: 'assistant.text.delta', sessionId: SESSION, messageId: 'm1', delta: 'it up.' }),
    )
    expect(transcriptOf(turn)).toEqual([{ type: 'assistant', message: { content: [{ type: 'text', text: 'Looking it up.' }] } }])
  })
})

describe('memory activities in a durable turn', () => {
  let fake: FakeHindsight
  let appended: ServerEvent[]
  let audits: AuditEntry[]

  beforeEach(async () => {
    fake = await startFakeHindsight()
    fake.memories = ['the box was red']
    appended = []
    audits = []
  })
  afterEach(async () => {
    await fake.close()
  })

  function deps(overrides: Partial<MemoryActivityDeps> = {}): MemoryActivityDeps {
    const forwarded: PluginsForRun = {
      plugins: [],
      problems: [],
      secrets: [`Bearer ${TOKEN}`, TOKEN],
      hindsight: {
        plugin: {
          name: 'hindsight',
          url: `http://hindsight.invalid:${fake.port}/mcp/${BANK}/`,
          header: { name: 'Authorization', value: `Bearer ${TOKEN}` },
          toolTiers: {},
          disabledTools: [],
        },
        address: '127.0.0.1',
      },
      release: () => {},
    }
    return {
      sessions: { ownerOf: async (id) => (id === SESSION ? BROWSER : undefined) },
      events: {
        ...readsOf(conversation),
        append: async (_id, events) => {
          appended.push(...events)
          return []
        },
      },
      remotePlugins: async () => forwarded,
      credentialSecrets: async () => [CREDENTIAL],
      audit: { record: async (entry) => void audits.push(entry) },
      log: () => {},
      ...overrides,
    }
  }

  const env = (activityType: string, workflowId = `session-${SESSION}`) =>
    new MockActivityEnvironment({ activityType, workflowExecution: { workflowId, runId: 'run-1' }, taskQueue: 'agent-tools' })

  it("recalls against the turn's own words and returns the classic block, recorded as classic records it", async () => {
    const acts = memoryActivities(deps())
    const { context } = (await env(MEMORY_RECALL_ACTIVITY).run(acts[MEMORY_RECALL_ACTIVITY], { turn_id: TURN })) as MemoryRecalled
    expect(context).toMatch(/^<hindsight_memories>/)
    expect(context).toContain('1. the box was red')
    // The query left redacted, as a classic turn's.
    expect(fake.recalls()[0]!.body).toMatchObject({ query: 'what colour was it? [redacted]' })
    expect(appended).toEqual([expect.objectContaining({ type: 'memory', action: 'recall', outcome: 'ok', count: 1, turnId: TURN })])
    expect(audits).toEqual([expect.objectContaining({ kind: 'memory', action: 'recall', sessionId: SESSION, turnId: TURN })])
  })

  it("finds the turn's words by its id, never reading the session's whole log (#2419)", async () => {
    const reads = readsOf(conversation)
    const acts = memoryActivities(
      deps({ events: { ...reads, read: () => Promise.reject(new Error('the log was read')), append: async () => [] } }),
    )
    const { context } = (await env(MEMORY_RECALL_ACTIVITY).run(acts[MEMORY_RECALL_ACTIVITY], { turn_id: TURN })) as MemoryRecalled
    expect(context).toContain('1. the box was red')
  })

  it('reads the recall query cap from Settings at each turn, as a classic turn does', async () => {
    const long = 'x'.repeat(1000)
    const events = log(event({ type: 'user.turn', sessionId: SESSION, turnId: TURN, text: long, author: BROWSER }))
    let stored: unknown
    const acts = memoryActivities(
      deps({
        events: { ...readsOf(events), append: async () => [] },
        settings: { get: async <T>(key: string) => (key === 'memory_recall_max_query_chars' ? (stored as T) : undefined) },
      }),
    )
    for (const value of [undefined, 30, 0]) {
      stored = value
      await env(MEMORY_RECALL_ACTIVITY).run(acts[MEMORY_RECALL_ACTIVITY], { turn_id: TURN })
    }
    expect(fake.recalls().map((r) => (r.body as { query: string }).query.length)).toEqual([800, 30, 1000])
  })

  it("retains the session's transcript as one document, redacted", async () => {
    const acts = memoryActivities(deps())
    await env(MEMORY_RETAIN_ACTIVITY).run(acts[MEMORY_RETAIN_ACTIVITY], { turn_id: TURN })
    const [retain] = fake.retains()
    const item = (retain!.body as { items: { content: string; document_id: string }[] }).items[0]!
    expect(item.document_id).toBe(`conversation:${SESSION}`)
    expect(item.content).toContain('make a box')
    expect(item.content).toContain('render_model box')
    expect(item.content).not.toContain(CREDENTIAL)
    expect(appended).toEqual([expect.objectContaining({ type: 'memory', action: 'retain', outcome: 'ok' })])
  })

  it('never fails the turn: a Hindsight that refuses is recorded, and no memories come back', async () => {
    fake.respond = () => ({ status: 500, text: `broken ${TOKEN}` })
    const acts = memoryActivities(deps())
    expect(await env(MEMORY_RECALL_ACTIVITY).run(acts[MEMORY_RECALL_ACTIVITY], { turn_id: TURN })).toEqual({ context: null })
    await env(MEMORY_RETAIN_ACTIVITY).run(acts[MEMORY_RETAIN_ACTIVITY], { turn_id: TURN })
    expect(appended.map((e) => e.type === 'memory' && [e.action, e.outcome])).toEqual([
      ['recall', 'error'],
      ['retain', 'error'],
    ])
    expect(JSON.stringify(appended)).not.toContain(TOKEN)
  })

  it('does nothing without a hindsight plugin, with memory off, or outside a durable session', async () => {
    const none = memoryActivities(deps({ remotePlugins: async () => ({ plugins: [], problems: [], secrets: [], release: () => {} }) }))
    expect(await env(MEMORY_RECALL_ACTIVITY).run(none[MEMORY_RECALL_ACTIVITY], { turn_id: TURN })).toEqual({ context: null })
    const off = memoryActivities(deps({ memory: false }))
    await env(MEMORY_RETAIN_ACTIVITY).run(off[MEMORY_RETAIN_ACTIVITY], { turn_id: TURN })
    const elsewhere = memoryActivities(deps())
    expect(await env(MEMORY_RECALL_ACTIVITY, 'flow-x').run(elsewhere[MEMORY_RECALL_ACTIVITY], { turn_id: TURN })).toEqual({ context: null })
    expect(fake.requests).toEqual([])
    expect(appended).toEqual([])
  })
})
