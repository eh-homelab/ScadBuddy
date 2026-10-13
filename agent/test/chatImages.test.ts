import { createHash } from 'node:crypto'
import type { SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { sentImageBlobs } from '../src/sessions/blobs.js'
import { parseClientFrame, STALE_TAB_IMAGES } from '../src/sessions/clientProtocol.js'
import {
  IMAGE_BYTES_MAX,
  IMAGES_BYTES_TOTAL_MAX,
  IMAGES_MAX,
  PREVIEW_BYTES_MAX,
  userPrompt,
  type UserImage,
  UserImagesSchema,
} from '../src/sessions/images.js'
import { event } from '../src/sessions/protocol.js'
import { condense } from '../src/tools/sessions.js'
import { frontendClientMessages, frontendParseServerEvent } from './support/frontendProtocol.js'

// #1866: images the user pastes, drops or attaches in the assistant panel.
// #2479: they are bytes and blobs; events name them, never carry them.

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1])
const GIF = Buffer.from('GIF89a\x01\x00\x01\x00\x00\x00\x00;', 'latin1')
const WEBP = Buffer.from('RIFF\x1a\x00\x00\x00WEBPVP8 ', 'latin1')
const sha = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex')

const image = (mediaType: string, bytes: Buffer): UserImage =>
  ({ mediaType, bytes, preview: { mediaType: 'image/jpeg', bytes: JPEG } }) as UserImage

/** What a tab loaded before #1941 sent: the image inline, as base64 in the frame. */
const inline = (mediaType: string, bytes: Buffer) => ({
  mediaType,
  data: bytes.toString('base64'),
  preview: { mediaType: 'image/jpeg', data: JPEG.toString('base64') },
})

const frame = (images: unknown, text = 'what is this?') =>
  JSON.stringify({ v: 1, type: 'user.message', text, context: { route: '/' }, images })

/** `size` bytes of a PNG: its signature, then padding. */
function pngOfSize(size: number): Buffer {
  const out = Buffer.alloc(size)
  PNG.copy(out, 0, 0, 8)
  return out
}

const ID = '0b6f7a1e-3c2d-4e5f-8a9b-0c1d2e3f4a5b'

describe('user.message images: uploads by id (#1941)', () => {
  it('takes a message with no images, as before', () => {
    expect(parseClientFrame(frame(undefined)).ok).toBe(true)
  })

  it('takes what the panel builds: uploads by id (#1941)', async () => {
    const { clientMessage, parseClientMessage } = await frontendClientMessages()
    const images = [{ kind: 'attachment', id: ID }]
    const built = clientMessage({ type: 'user.message', text: 'look', context: { route: '/' }, images })
    expect(parseClientFrame(JSON.stringify(built))).toMatchObject({ ok: true, value: { images } })
    // The panel no longer builds inline images.
    expect(parseClientMessage(frame([inline('image/png', PNG)])).ok).toBe(false)
  })

  it('takes up to IMAGES_MAX attachment ids, and nothing else beside them (#1941)', () => {
    const ref = (id = ID) => ({ kind: 'attachment', id })
    expect(parseClientFrame(frame(Array.from({ length: IMAGES_MAX }, () => ref()))).ok).toBe(true)
    expect(parseClientFrame(frame(Array.from({ length: IMAGES_MAX + 1 }, () => ref()))).ok).toBe(false)
    expect(parseClientFrame(frame([ref('not-a-uuid')])).ok).toBe(false)
    expect(parseClientFrame(frame([{ ...ref(), extra: 1 }])).ok).toBe(false)
    expect(parseClientFrame(frame([ref(), inline('image/png', PNG)])).ok).toBe(false)
    expect(parseClientFrame(frame([{ kind: 'asset', slug: 'keychain', asset_id: 'a'.repeat(64) }])).ok).toBe(false)
    expect(parseClientFrame(frame([])).ok).toBe(false)
  })

  it('refuses inline images from a tab loaded before #1941, asking for a reload (#1959)', () => {
    expect(STALE_TAB_IMAGES).toContain('reload the page')
    expect(parseClientFrame(frame([inline('image/png', PNG)]))).toEqual({ ok: false, error: STALE_TAB_IMAGES, stale: {} })
    // One that would never have passed gets the same answer, which never quotes its bytes.
    expect(parseClientFrame(frame([inline('image/png', JPEG)]))).toEqual({ ok: false, error: STALE_TAB_IMAGES, stale: {} })
    // A message to a session names it, so the panel shows the refusal in that session's feed.
    const sessionId = '11111111-2222-4333-8444-555555555555'
    const inSession = JSON.stringify({ ...(JSON.parse(frame([inline('image/png', PNG)])) as object), sessionId })
    expect(parseClientFrame(inSession)).toEqual({ ok: false, error: STALE_TAB_IMAGES, stale: { sessionId } })
  })
})

// UserImagesSchema checks each upload (routes/attachments.ts) and a message's
// images together (routes/chat.ts, tools/imageRefs.ts), as bytes (#2479).
describe('UserImagesSchema (#1866, #2479)', () => {
  const ok = (images: unknown) => UserImagesSchema.safeParse(images).success

  it('takes PNG, JPEG, GIF and WebP images, each with a preview', () => {
    expect(ok([image('image/png', PNG), image('image/jpeg', JPEG), image('image/gif', GIF), image('image/webp', WEBP)])).toBe(true)
  })

  it('refuses another type, bytes that are not the type they claim, base64 text, and nothing', () => {
    expect(ok([image('image/svg+xml', Buffer.from('<svg/>'))])).toBe(false)
    expect(ok([image('image/bmp', Buffer.from('BM\x00\x00'))])).toBe(false)
    expect(ok([image('image/png', JPEG)])).toBe(false)
    expect(ok([image('image/jpeg', Buffer.from('<html>not an image</html>'))])).toBe(false)
    expect(ok([{ ...image('image/png', PNG), bytes: PNG.toString('base64') }])).toBe(false)
    expect(ok([image('image/png', Buffer.alloc(0))])).toBe(false)
  })

  it('refuses a preview that is missing, too large, a GIF, or not what it claims', () => {
    expect(ok([{ mediaType: 'image/png', bytes: PNG }])).toBe(false)
    const big = { ...image('image/png', PNG), preview: { mediaType: 'image/png', bytes: pngOfSize(PREVIEW_BYTES_MAX + 1) } }
    expect(ok([big])).toBe(false)
    const gif = { ...image('image/png', PNG), preview: { mediaType: 'image/gif', bytes: GIF } }
    expect(ok([gif])).toBe(false)
    const lying = { ...image('image/png', PNG), preview: { mediaType: 'image/png', bytes: JPEG } }
    expect(ok([lying])).toBe(false)
  })

  it('caps the count, each image, and all of them together, in bytes', () => {
    expect(ok([])).toBe(false)
    expect(ok(Array.from({ length: IMAGES_MAX }, () => image('image/png', PNG)))).toBe(true)
    expect(ok(Array.from({ length: IMAGES_MAX + 1 }, () => image('image/png', PNG)))).toBe(false)
    expect(ok([image('image/png', pngOfSize(IMAGE_BYTES_MAX))])).toBe(true)
    expect(ok([image('image/png', pngOfSize(IMAGE_BYTES_MAX + 1))])).toBe(false)
    const each = Math.floor(IMAGES_BYTES_TOTAL_MAX / 2) + 1
    expect(each).toBeLessThanOrEqual(IMAGE_BYTES_MAX)
    expect(ok([image('image/png', pngOfSize(each)), image('image/png', pngOfSize(each))])).toBe(false)
  })

  it('keeps the Messages API bounds: 5 MiB of base64 an image, 8 MiB together', () => {
    expect(IMAGE_BYTES_MAX).toBe(((5 * 1024 * 1024) / 4) * 3)
    expect(IMAGES_BYTES_TOTAL_MAX).toBe(((8 * 1024 * 1024) / 4) * 3)
  })

  it('never echoes the image bytes in its refusal', () => {
    const parsed = UserImagesSchema.safeParse([image('image/png', JPEG)])
    expect(parsed.success).toBe(false)
    if (!parsed.success) expect(z.prettifyError(parsed.error)).not.toContain(JPEG.toString('base64'))
  })
})

describe('userPrompt (#1866)', () => {
  async function all(prompt: AsyncIterable<SDKUserMessage>): Promise<SDKUserMessage[]> {
    const out: SDKUserMessage[] = []
    for await (const m of prompt) out.push(m)
    return out
  }

  it('is one user message: the images as base64 image blocks, then the text (the SDK boundary, #2479)', async () => {
    const prompt = userPrompt('what is this?\n\n<page_context>…', [image('image/png', PNG), image('image/gif', GIF)])
    const [message, ...rest] = await all(prompt)
    expect(rest).toEqual([])
    expect(message).toMatchObject({ type: 'user', parent_tool_use_id: null })
    expect(message?.message).toEqual({
      role: 'user',
      content: [
        { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG.toString('base64') } },
        { type: 'image', source: { type: 'base64', media_type: 'image/gif', data: GIF.toString('base64') } },
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

describe('user.turn images (#1866, #2479)', () => {
  const S = '11111111-2222-4333-8444-555555555555'
  const browser = { kind: 'browser' as const, id: 'browser', label: 'You' }

  it('are pointers to blobs, the full image and its preview, which the panel parses', async () => {
    const { blobs, sent } = sentImageBlobs([image('image/png', PNG)])
    const full = `${sha(PNG)}.png`
    const small = `${sha(JPEG)}.jpg`
    expect(sent).toEqual([{ name: full, mediaType: 'image/png', preview: { name: small, mediaType: 'image/jpeg' } }])
    expect(blobs.map((b) => b.name)).toEqual([full, small])
    const parse = await frontendParseServerEvent()
    const turn = event({ type: 'user.turn', sessionId: S, turnId: 't', text: 'look', author: browser, images: sent })
    expect(parse(turn)).toMatchObject({ ok: true, value: { images: sent } })
    expect(JSON.stringify(turn)).not.toContain(PNG.toString('base64').slice(0, 16))
    expect(JSON.stringify(turn)).not.toContain(JPEG.toString('base64').slice(0, 12))
  })

  it('the panel refuses one that carries bytes inline', async () => {
    const parse = await frontendParseServerEvent()
    const [sent] = sentImageBlobs([image('image/png', PNG)]).sent
    const turn = event({
      type: 'user.turn',
      sessionId: S,
      turnId: 't',
      text: 'look',
      author: browser,
      images: [{ ...sent!, data: JPEG.toString('base64') } as never],
    })
    expect(parse(turn).ok).toBe(false)
  })

  it('are a count in an MCP transcript', () => {
    const { sent } = sentImageBlobs([image('image/png', PNG)])
    const turn = event({ type: 'user.turn', sessionId: S, turnId: 't', text: 'look', author: browser, images: sent })
    const [row] = condense([{ seq: 1, event: turn }])
    expect(row).toEqual({ seq: 1, type: 'user.turn', turnId: 't', text: 'look', author: browser, images: 1 })
  })
})
