import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import { event, type ServerEvent, type ServerEventType } from '../src/sessions/protocol.js'
import { INPUT_MAX, REDACTED, scrubForLog, SdkEventMapper, SUMMARY_MAX } from '../src/sessions/sdkEvents.js'
import { expectPanelAccepts, frontendParseServerEvent } from './support/frontendProtocol.js'

const S = '11111111-2222-4333-8444-555555555555'
const tiers = (name: string) => (name === 'mcp__scadbuddy__catalogue_list' ? ('read' as const) : undefined)

const stream = (e: Record<string, unknown>, parent: string | null = null) =>
  ({ type: 'stream_event', event: e, parent_tool_use_id: parent, uuid: 'u', session_id: S }) as unknown as SDKMessage
const assistant = (id: string, content: unknown[], parent: string | null = null) =>
  ({ type: 'assistant', message: { id, content }, parent_tool_use_id: parent, session_id: S, uuid: 'a' }) as unknown as SDKMessage
const user = (content: unknown, extra: Record<string, unknown> = {}) =>
  ({ type: 'user', message: { role: 'user', content }, parent_tool_use_id: null, session_id: S, ...extra }) as unknown as SDKMessage

function mapAll(messages: SDKMessage[]): ServerEvent[] {
  const mapper = new SdkEventMapper(S, tiers)
  return messages.flatMap((m) => mapper.map(m))
}

describe('SdkEventMapper', () => {
  it('maps streamed text to deltas and done, in the order the SDK yields it', () => {
    // The order measured on SDK 0.3.283: the complete assistant message comes
    // before its content_block_stop.
    const events = mapAll([
      { type: 'system', subtype: 'init', session_id: S } as unknown as SDKMessage,
      stream({ type: 'message_start', message: { id: 'msg_1' } }),
      stream({ type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }),
      stream({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'Hel' } }),
      stream({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'lo' } }),
      assistant('msg_1', [{ type: 'text', text: 'Hello' }]),
      stream({ type: 'content_block_stop', index: 0 }),
      stream({ type: 'message_delta', delta: { stop_reason: 'end_turn' } }),
      stream({ type: 'message_stop' }),
      { type: 'result', subtype: 'success', total_cost_usd: 0.1, num_turns: 1 } as unknown as SDKMessage,
    ])
    expect(events).toEqual([
      event({ type: 'assistant.text.delta', sessionId: S, messageId: 'msg_1:0', delta: 'Hel' }),
      event({ type: 'assistant.text.delta', sessionId: S, messageId: 'msg_1:0', delta: 'lo' }),
      event({ type: 'assistant.text.done', sessionId: S, messageId: 'msg_1:0' }),
    ])
  })

  it('falls back to the complete message’s text when nothing was streamed', () => {
    expect(mapAll([assistant('msg_2', [{ type: 'text', text: 'Whole' }])])).toEqual([
      event({ type: 'assistant.text.delta', sessionId: S, messageId: 'msg_2:0', delta: 'Whole' }),
      event({ type: 'assistant.text.done', sessionId: S, messageId: 'msg_2:0' }),
    ])
  })

  it('maps tool_use to tool.call with its tier, unknown tools as outward, once per id', () => {
    const events = mapAll([
      stream({ type: 'message_start', message: { id: 'msg_3' } }),
      stream({ type: 'content_block_start', index: 0, content_block: { type: 'tool_use', id: 't1', name: 'x' } }),
      stream({ type: 'content_block_delta', index: 0, delta: { type: 'input_json_delta', partial_json: '{}' } }),
      assistant('msg_3', [{ type: 'tool_use', id: 't1', name: 'mcp__scadbuddy__catalogue_list', input: { q: 'box' } }]),
      assistant('msg_3', [{ type: 'tool_use', id: 't1', name: 'mcp__scadbuddy__catalogue_list', input: { q: 'box' } }]),
      stream({ type: 'content_block_stop', index: 0 }),
      assistant('msg_4', [{ type: 'tool_use', id: 't2', name: 'mcp__plugin__mystery', input: {} }]),
    ])
    expect(events).toEqual([
      event({ type: 'tool.call', sessionId: S, id: 't1', name: 'mcp__scadbuddy__catalogue_list', input: { q: 'box' }, risk: 'read' }),
      event({ type: 'tool.call', sessionId: S, id: 't2', name: 'mcp__plugin__mystery', input: {}, risk: 'outward' }),
    ])
  })

  it('maps tool_result to tool.result with a bounded summary, skipping replays and subagents', () => {
    const long = 'x'.repeat(SUMMARY_MAX + 50)
    const events = mapAll([
      user([{ type: 'tool_result', tool_use_id: 't1', content: [{ type: 'text', text: 'three models' }] }]),
      user([{ type: 'tool_result', tool_use_id: 't2', is_error: true, content: 'needs approval' }]),
      user([{ type: 'tool_result', tool_use_id: 't3', content: long }]),
      user([{ type: 'tool_result', tool_use_id: 't4', content: 'old' }], { isReplay: true }),
      user('a plain prompt'),
      stream({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'sub' } }, 'toolu_parent'),
    ])
    expect(events.map((e) => (e.type === 'tool.result' ? [e.id, e.ok, e.summary.length] : e.type))).toEqual([
      ['t1', true, 'three models'.length],
      ['t2', false, 'needs approval'.length],
      ['t3', true, SUMMARY_MAX],
    ])
  })
})

describe('scrubForLog', () => {
  const SECRET = 'sk-ant-api03-scrub-test-0000'

  it('blanks secret-named arguments at any depth and redacts the credential everywhere', () => {
    const call = event({
      type: 'tool.call',
      sessionId: S,
      id: 't1',
      name: 'mcp__x__login',
      input: {
        user: 'me',
        password: 'hunter2',
        nested: { apiKey: 'k1', api_key: 'k2', headers: [{ Authorization: 'Bearer abc' }] },
        note: `uses ${SECRET}`,
        author: 'kept',
      },
      risk: 'outward',
    })
    expect(scrubForLog(call, [SECRET])).toEqual({
      ...call,
      input: {
        user: 'me',
        password: REDACTED,
        nested: { apiKey: REDACTED, api_key: REDACTED, headers: [{ Authorization: REDACTED }] },
        note: 'uses [redacted]',
        author: 'kept',
      },
    })
    // The event itself is not mutated.
    expect(call.type === 'tool.call' && call.input.password).toBe('hunter2')
  })

  it('cuts an oversized input to a preview', () => {
    const call = event({ type: 'tool.call', sessionId: S, id: 't', name: 'n', input: { blob: 'x'.repeat(INPUT_MAX * 2) }, risk: 'read' })
    const out = scrubForLog(call, [])
    if (out.type !== 'tool.call') throw new Error('unreachable')
    expect(out.input.truncated).toBe(true)
    expect(String(out.input.preview)).toHaveLength(INPUT_MAX)
  })

  it('redacts the credential from results, text and errors, and leaves ids alone', () => {
    const events = [
      event({ type: 'tool.result', sessionId: S, id: 't', ok: false, summary: `rejected ${SECRET}` }),
      event({ type: 'assistant.text.delta', sessionId: S, messageId: 'm:0', delta: SECRET }),
      event({ type: 'error', sessionId: S, message: `bad ${SECRET}` }),
    ]
    expect(JSON.stringify(events.map((e) => scrubForLog(e, [SECRET])))).not.toContain(SECRET)
    expect(scrubForLog(events[0]!, [SECRET])).toMatchObject({ sessionId: S, id: 't', summary: 'rejected [redacted]' })
  })
})

describe('the agent’s protocol mirror', () => {
  const sample: ServerEvent[] = [
    event({ type: 'sessions.snapshot', sessions: [{ sessionId: S, title: 't', origin: 'mcp', owner: { kind: 'bearer', id: 'token:a', label: 'A' }, status: 'idle' }] }),
    event({ type: 'session.started', sessionId: S, origin: 'chat', owner: { kind: 'browser', id: 'browser', label: 'You' }, title: '' }),
    event({ type: 'session.owner', sessionId: S, owner: { kind: 'browser', id: 'browser', label: 'You' } }),
    event({ type: 'user.turn', sessionId: S, turnId: 't', text: 'hi', author: { kind: 'flow', id: 'analyzer', label: 'Analyzer' } }),
    event({ type: 'assistant.text.delta', sessionId: S, messageId: 'm:0', delta: 'x' }),
    event({ type: 'assistant.text.done', sessionId: S, messageId: 'm:0' }),
    event({ type: 'tool.call', sessionId: S, id: 't1', name: 'n', input: {}, risk: 'write' }),
    event({ type: 'tool.result', sessionId: S, id: 't1', ok: true, summary: '' }),
    event({ type: 'approval.required', sessionId: S, id: 'a1', tool: 't1', summary: 'print box.3mf', risk: 'outward' }),
    event({ type: 'approval.resolved', sessionId: S, id: 'a1', approved: true, by: { kind: 'browser', id: 'browser', label: 'You' } }),
    event({ type: 'approval.resolved', sessionId: S, id: 'a2', approved: false }),
    event({ type: 'session.status', sessionId: S, status: 'waiting_approval' }),
    event({ type: 'session.result', sessionId: S, costUsd: 0.5, turns: 3 }),
    event({ type: 'error', sessionId: S, code: 'interrupted', message: 'the turn was interrupted' }),
  ]

  it('produces events the panel’s own schema accepts', async () => {
    await expectPanelAccepts(sample)
    // …and that schema is really in force: a bad status is dropped.
    const parse = await frontendParseServerEvent()
    expect(parse({ v: 1, type: 'session.status', sessionId: S, status: 'paused' }).ok).toBe(false)
  })

  it('covers every server event type the panel declares', async () => {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const source = await readFile(path.resolve(here, '../../frontend/src/agent/chat/protocol.ts'), 'utf8')
    const serverPart = source.slice(source.indexOf('ServerEventSchema'), source.indexOf('ClientMessageSchema'))
    const declared = [...serverPart.matchAll(/type: z\.literal\('([^']+)'\)/g)].map((m) => m[1])
    const mirrored: ServerEventType[] = sample.map((e) => e.type)
    expect(declared.filter((t) => !mirrored.includes(t as ServerEventType))).toEqual([])
  })
})
