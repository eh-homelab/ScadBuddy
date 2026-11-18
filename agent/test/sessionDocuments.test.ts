import { describe, expect, it } from 'vitest'
import { BLOB_NAME, sentImageBlobs, sessionImage } from '../src/sessions/blobs.js'
import { checkedAttachments, PDF_BYTES_MAX, userPrompt, type UserImage } from '../src/sessions/images.js'

// A session's first message can carry an invoice PDF (#6267): a `document` block at the SDK
// boundary, the images unchanged, anything else refused.

const PDF = Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.from('invoice')])
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(16)])
const image: UserImage = { mediaType: 'image/png', bytes: PNG, preview: { mediaType: 'image/png', bytes: PNG } }
const pdf = { mediaType: 'application/pdf' as const, bytes: PDF }

async function content(prompt: AsyncIterable<{ message: { content: unknown } }>) {
  for await (const m of prompt) return m.message.content
  throw new Error('no message')
}

describe('userPrompt', () => {
  it('builds a document block for a PDF, before the text', async () => {
    expect(await content(userPrompt('words', [pdf]))).toEqual([
      { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: PDF.toString('base64') } },
      { type: 'text', text: 'words' },
    ])
  })

  it('keeps an image an image block', async () => {
    expect(await content(userPrompt('words', [image]))).toEqual([
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG.toString('base64') } },
      { type: 'text', text: 'words' },
    ])
  })
})

describe('checkedAttachments', () => {
  it('accepts a PDF alone, or beside an image', () => {
    expect(checkedAttachments([pdf]).ok).toBe(true)
    expect(checkedAttachments([image, pdf]).ok).toBe(true)
  })

  it.each([
    ['a text file', { mediaType: 'text/plain', bytes: Buffer.from('hello') }],
    ['a HEIC file', { mediaType: 'image/heic', bytes: Buffer.from('....ftypheic') }],
    ['bytes that are not a PDF', { mediaType: 'application/pdf', bytes: Buffer.from('hello world') }],
    ['an empty PDF', { mediaType: 'application/pdf', bytes: Buffer.alloc(0) }],
  ])('refuses %s', (_, attachment) => {
    expect(checkedAttachments([attachment]).ok).toBe(false)
  })

  it('refuses a PDF over the cap, naming the cap and not the bytes', () => {
    const big = { mediaType: 'application/pdf', bytes: Buffer.concat([Buffer.from('%PDF-'), Buffer.alloc(PDF_BYTES_MAX)]) }
    const checked = checkedAttachments([big])
    expect(checked.ok).toBe(false)
    if (!checked.ok) expect(checked.error).toContain(`${PDF_BYTES_MAX}`)
  })
})

describe('a PDF in the session blobs', () => {
  it('is named by its sha256 and .pdf, and a sent turn names it without a preview', () => {
    const blob = sessionImage('application/pdf', PDF)
    expect(blob.name).toMatch(BLOB_NAME)
    expect(blob.name.endsWith('.pdf')).toBe(true)
    const { blobs, sent } = sentImageBlobs([pdf])
    expect(blobs).toEqual([blob])
    expect(sent).toEqual([{ name: blob.name, mediaType: 'application/pdf' }])
  })
})
