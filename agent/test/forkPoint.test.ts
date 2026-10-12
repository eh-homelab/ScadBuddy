import { describe, expect, it } from 'vitest'
import { blobNames, transcriptCut } from '../src/sessions/forkPoint.js'
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
