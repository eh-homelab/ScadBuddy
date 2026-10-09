import type { SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import { parseClientFrame } from '../src/sessions/clientProtocol.js'
import {
  IMAGE_DATA_MAX,
  IMAGES_DATA_TOTAL_MAX,
  IMAGES_MAX,
  PREVIEW_DATA_MAX,
  previewsOf,
  userPrompt,
  type UserImage,
} from '../src/sessions/images.js'
import { event } from '../src/sessions/protocol.js'
import { condense } from '../src/tools/sessions.js'
import { frontendClientMessages, frontendParseServerEvent } from './support/frontendProtocol.js'

// #1866: images the user pastes, drops or attaches in the assistant panel.

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
const b64 = (bytes: number[] | string) => Buffer.from(typeof bytes === 'string' ? bytes : Uint8Array.from(bytes)).toString('base64')
const JPEG = b64([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1])
const GIF = b64('GIF89a\x01\x00\x01\x00\x00\x00\x00;')
const WEBP = b64('RIFF\x1a\x00\x00\x00WEBPVP8 ')

const image = (mediaType: string, data: string): UserImage =>
  ({ mediaType, data, preview: { mediaType: 'image/jpeg', data: JPEG } }) as UserImage

const frame = (images: unknown, text = 'what is this?') =>
  JSON.stringify({ v: 1, type: 'user.message', text, context: { route: '/' }, images })

/** `count` base64 characters of a PNG: its signature, then padding bytes. */
function pngOfLength(count: number): string {
  const head = PNG.slice(0, 12)
  return head + 'A'.repeat(count - head.length)
}

const ID = '0b6f7a1e-3c2d-4e5f-8a9b-0c1d2e3f4a5b'

describe('user.message images (#1866; inline ones from a tab loaded before #1941)', () => {
  it('takes PNG, JPEG, GIF and WebP images, each with a preview', () => {
    const images = [image('image/png', PNG), image('image/jpeg', JPEG), image('image/gif', GIF), image('image/webp', WEBP)]
    const parsed = parseClientFrame(frame(images))
    expect(parsed).toMatchObject({ ok: true, value: { images } })
  })

  it('takes a message with no images, as before', () => {
    expect(parseClientFrame(frame(undefined)).ok).toBe(true)
  })

  it('takes what the panel builds: uploads by id (#1941)', async () => {
    const { clientMessage, parseClientMessage } = await frontendClientMessages()
    const images = [{ kind: 'attachment', id: ID }]
    const built = clientMessage({ type: 'user.message', text: 'look', context: { route: '/' }, images })
    expect(parseClientFrame(JSON.stringify(built))).toMatchObject({ ok: true, value: { images } })
    // The panel no longer builds inline images.
    expect(parseClientMessage(frame([image('image/png', PNG)])).ok).toBe(false)
  })

  it('takes up to IMAGES_MAX attachment ids, and nothing else beside them (#1941)', () => {
    const ref = (id = ID) => ({ kind: 'attachment', id })
    expect(parseClientFrame(frame(Array.from({ length: IMAGES_MAX }, () => ref()))).ok).toBe(true)
    expect(parseClientFrame(frame(Array.from({ length: IMAGES_MAX + 1 }, () => ref()))).ok).toBe(false)
    expect(parseClientFrame(frame([ref('not-a-uuid')])).ok).toBe(false)
    expect(parseClientFrame(frame([{ ...ref(), extra: 1 }])).ok).toBe(false)
    expect(parseClientFrame(frame([ref(), image('image/png', PNG)])).ok).toBe(false)
    expect(parseClientFrame(frame([{ kind: 'asset', slug: 'keychain', asset_id: 'a'.repeat(64) }])).ok).toBe(false)
  })

  it('refuses another type, bytes that are not the type they claim, and data that is not base64', () => {
    expect(parseClientFrame(frame([image('image/svg+xml', b64('<svg/>'))])).ok).toBe(false)
    expect(parseClientFrame(frame([image('image/bmp', b64('BM\x00\x00'))])).ok).toBe(false)
    expect(parseClientFrame(frame([image('image/png', JPEG)])).ok).toBe(false)
    expect(parseClientFrame(frame([image('image/jpeg', b64('<html>not an image</html>'))])).ok).toBe(false)
    expect(parseClientFrame(frame([image('image/png', `${PNG.slice(0, 12)}!!!!`)])).ok).toBe(false)
    expect(parseClientFrame(frame([image('image/png', '')])).ok).toBe(false)
  })

  it('refuses a preview that is missing, too large, a GIF, or not what it claims', () => {
    expect(parseClientFrame(frame([{ mediaType: 'image/png', data: PNG }])).ok).toBe(false)
    const big = { ...image('image/png', PNG), preview: { mediaType: 'image/png', data: pngOfLength(PREVIEW_DATA_MAX + 4) } }
    expect(parseClientFrame(frame([big])).ok).toBe(false)
    const gif = { ...image('image/png', PNG), preview: { mediaType: 'image/gif', data: GIF } }
    expect(parseClientFrame(frame([gif])).ok).toBe(false)
    const lying = { ...image('image/png', PNG), preview: { mediaType: 'image/png', data: JPEG } }
    expect(parseClientFrame(frame([lying])).ok).toBe(false)
  })

  it('caps the count, each image, and all of them together', () => {
    expect(parseClientFrame(frame([])).ok).toBe(false)
    expect(parseClientFrame(frame(Array.from({ length: IMAGES_MAX }, () => image('image/png', PNG)))).ok).toBe(true)
    expect(parseClientFrame(frame(Array.from({ length: IMAGES_MAX + 1 }, () => image('image/png', PNG)))).ok).toBe(false)
    expect(parseClientFrame(frame([image('image/png', pngOfLength(IMAGE_DATA_MAX))])).ok).toBe(true)
    expect(parseClientFrame(frame([image('image/png', pngOfLength(IMAGE_DATA_MAX + 4))])).ok).toBe(false)
    const each = Math.floor(IMAGES_DATA_TOTAL_MAX / 2 / 4) * 4 + 4
    expect(each).toBeLessThanOrEqual(IMAGE_DATA_MAX)
    expect(parseClientFrame(frame([image('image/png', pngOfLength(each)), image('image/png', pngOfLength(each))])).ok).toBe(false)
  })

  it('never echoes the image bytes in its refusal', () => {
    const parsed = parseClientFrame(frame([image('image/png', JPEG)]))
    expect(parsed.ok).toBe(false)
    if (!parsed.ok) expect(parsed.error).not.toContain(JPEG)
  })
})

describe('userPrompt (#1866)', () => {
  async function all(prompt: AsyncIterable<SDKUserMessage>): Promise<SDKUserMessage[]> {
    const out: SDKUserMessage[] = []
    for await (const m of prompt) out.push(m)
    return out
  }

  it('is one user message: the images as base64 image blocks, then the text', async () => {
    const prompt = userPrompt('what is this?\n\n<page_context>…', [image('image/png', PNG), image('image/gif', GIF)])
    const [message, ...rest] = await all(prompt)
    expect(rest).toEqual([])
    expect(message).toMatchObject({ type: 'user', parent_tool_use_id: null })
    expect(message?.message).toEqual({
      role: 'user',
      content: [
        { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG } },
        { type: 'image', source: { type: 'base64', media_type: 'image/gif', data: GIF } },
        { type: 'text', text: 'what is this?\n\n<page_context>…' },
      ],
    })
  })

  it('can be iterated again, for a credential fallback that runs the query again as it was', async () => {
    const prompt = userPrompt('again', [image('image/png', PNG)])
    expect(await all(prompt)).toEqual(await all(prompt))
    expect(await all(prompt)).toHaveLength(1)
  })
})

describe('user.turn images (#1866)', () => {
  const S = '11111111-2222-4333-8444-555555555555'
  const browser = { kind: 'browser' as const, id: 'browser', label: 'You' }

  it('carry only the previews, which the panel parses', async () => {
    const previews = previewsOf([image('image/png', PNG)])
    expect(previews).toEqual([{ mediaType: 'image/jpeg', data: JPEG }])
    const parse = await frontendParseServerEvent()
    const turn = event({ type: 'user.turn', sessionId: S, turnId: 't', text: 'look', author: browser, images: previews })
    expect(parse(turn)).toMatchObject({ ok: true, value: { images: previews } })
  })

  it('are a count in an MCP transcript, never bytes', () => {
    const turn = event({
      type: 'user.turn',
      sessionId: S,
      turnId: 't',
      text: 'look',
      author: browser,
      images: [{ mediaType: 'image/jpeg', data: JPEG }],
    })
    const [row] = condense([{ seq: 1, event: turn }])
    expect(row).toEqual({ seq: 1, type: 'user.turn', turnId: 't', text: 'look', author: browser, images: 1 })
    expect(JSON.stringify(row)).not.toContain(JPEG)
  })
})
