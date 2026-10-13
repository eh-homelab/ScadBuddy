import { describe, expect, it } from 'vitest'
import { blobNames, DURABLE_TEXT_MAX, durableReplies, entriesThrough, transcriptCut } from '../src/sessions/forkPoint.js'
import type { ServerEvent } from '../src/sessions/protocol.js'

// Fork from a message (#793): the panel's assistant message id (`<API message id>:<block>`,
// sessions/sdkEvents.ts) to the SDK transcript entry `forkSession({upToMessageId})` slices at.

const user = (uuid: string, content: unknown) => ({ type: 'user', uuid, message: { role: 'user', content } })
const assistant = (uuid: string, id: string, block: Record<string, unknown>, extra: Record<string, unknown> = {}) => ({
  type: 'assistant',
  uuid,
  message: { id, role: 'assistant', content: [block] },
  ...extra,
})

const transcript = [
  { type: 'queue-operation' },
  user('u1', 'make a box'),
  assistant('a1', 'msg_1', { type: 'thinking', thinking: '…' }),
  assistant('a2', 'msg_1', { type: 'text', text: 'a box' }),
  assistant('a3', 'msg_1', { type: 'tool_use', id: 't1', name: 'x', input: {} }),
  user('u2', [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }]),
  assistant('a4', 'msg_2', { type: 'text', text: 'first' }),
  assistant('a5', 'msg_2', { type: 'text', text: 'second' }),
  assistant('s1', 'msg_3', { type: 'text', text: 'a subagent' }, { isSidechain: true }),
  user('u3', 'taller'),
  assistant('a6', 'msg_4', { type: 'text', text: 'taller box' }),
]

describe('transcriptCut', () => {
  it("finds the entry of the message's text block, by the block's index", () => {
    expect(transcriptCut(transcript, 'msg_1:1')).toBe('a2')
    expect(transcriptCut(transcript, 'msg_2:0')).toBe('a4')
    expect(transcriptCut(transcript, 'msg_2:1')).toBe('a5')
    expect(transcriptCut(transcript, 'msg_4:0')).toBe('a6')
  })

  it("falls back to the message's last text entry when the blocks were not written one per entry", () => {
    expect(transcriptCut(transcript, 'msg_1:7')).toBe('a2')
    expect(transcriptCut(transcript, 'msg_1:0')).toBe('a2')
  })

  it('never cuts at a subagent’s message, an unknown one, or a malformed id', () => {
    expect(transcriptCut(transcript, 'msg_3:0')).toBeUndefined()
    expect(transcriptCut(transcript, 'msg_9:0')).toBeUndefined()
    expect(transcriptCut(transcript, 'msg_1')).toBeUndefined()
    expect(transcriptCut(transcript, ':0')).toBeUndefined()
    expect(transcriptCut([], 'msg_1:0')).toBeUndefined()
  })
})

describe('blobNames (#2150)', () => {
  const owner = { kind: 'browser', id: 'browser', label: 'You' }
  it("names a tool result's images and a user's sent images and their previews once each, and nothing else", () => {
    const a = `${'a'.repeat(64)}.png`
    const b = `${'b'.repeat(64)}.jpg`
    const c = `${'c'.repeat(64)}.jpg`
    const d = `${'d'.repeat(64)}.png`
    const events = [
      { type: 'user.turn', sessionId: 's', turnId: 't', text: 'x', author: owner, images: [
        { name: b, mediaType: 'image/jpeg', preview: { name: c, mediaType: 'image/jpeg' } },
        // A turn logged before #2479 whose full image could not be stored: its preview only.
        { preview: { name: d, mediaType: 'image/png' } },
      ] },
      { type: 'tool.result', sessionId: 's', id: '1', ok: true, summary: '', images: [{ name: a, mediaType: 'image/png' }] },
      { type: 'tool.result', sessionId: 's', id: '2', ok: true, summary: '', images: [{ name: a, mediaType: 'image/png' }] },
      { type: 'tool.result', sessionId: 's', id: '3', ok: true, summary: '' },
      { type: 'assistant.text.done', sessionId: 's', messageId: 'm:0' },
    ] as ServerEvent[]
    expect(blobNames(events).sort()).toEqual([a, b, c, d])
    expect(blobNames([])).toEqual([])
  })
})

describe('durableReplies (#2688)', () => {
  const text = (messageId: string, delta: string): ServerEvent[] => [
    { v: 1, type: 'assistant.text.delta', sessionId: 's', messageId, delta },
    { v: 1, type: 'assistant.text.done', sessionId: 's', messageId },
  ]

  it('matches each logged reply to the transcript entry holding its text, in order', () => {
    const events = [...text('durable-1', 'a box'), ...text('durable-4', 'first'), ...text('durable-5', 'second'), ...text('durable-9', 'taller box')]
    expect([...durableReplies(transcript, events)]).toEqual([
      ['durable-1', 'a2'],
      ['durable-4', 'a4'],
      ['durable-5', 'a5'],
      ['durable-9', 'a6'],
    ])
  })

  it("leaves out a reply that never committed (a retried attempt's, a stopped turn's), and a subagent's", () => {
    const events = [
      ...text('durable-1', 'a bo'), // a retried attempt's, superseded
      ...text('durable-2', 'a box'),
      ...text('durable-4', 'first'),
      ...text('durable-5', 'second'),
      ...text('durable-6', 'a subagent'),
      ...text('durable-7', 'stopped before it committed'),
      ...text('durable-9', 'taller box'),
    ]
    expect([...durableReplies(transcript, events)]).toEqual([
      ['durable-2', 'a2'],
      ['durable-4', 'a4'],
      ['durable-5', 'a5'],
      ['durable-9', 'a6'],
    ])
  })

  it('skips an entry no event names, but never jumps to a text another entry also holds', () => {
    const twice = [...transcript, user('u4', 'again'), assistant('a7', 'msg_5', { type: 'text', text: 'first' })]
    // `a box` was never logged: `first` has two entries left, so it is not guessed at.
    expect([...durableReplies(twice, [...text('durable-4', 'first'), ...text('durable-9', 'taller box')])]).toEqual([['durable-9', 'a6']])
    expect([...durableReplies(transcript, text('durable-9', 'taller box'))]).toEqual([['durable-9', 'a6']])
  })

  it('matches a reply the log holds cut at the cap', () => {
    const long = 'x'.repeat(DURABLE_TEXT_MAX + 10)
    const entries = [user('u1', 'go'), assistant('a1', 'm', { type: 'text', text: long })]
    expect([...durableReplies(entries, text('durable-1', long.slice(0, DURABLE_TEXT_MAX)))]).toEqual([['durable-1', 'a1']])
    expect([...durableReplies(entries, text('durable-1', long.slice(0, DURABLE_TEXT_MAX - 1)))]).toEqual([])
  })

  it('entriesThrough cuts after the entry, and finds no sidechain or unknown one', () => {
    expect(entriesThrough(transcript, 'a4')?.map((e) => e.uuid)).toEqual([undefined, 'u1', 'a1', 'a2', 'a3', 'u2', 'a4'])
    expect(entriesThrough(transcript, 's1')).toBeUndefined()
    expect(entriesThrough(transcript, 'nope')).toBeUndefined()
  })
})
