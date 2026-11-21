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

describe('a refusal for a type ScadBuddy cannot read', () => {
  it('speaks to the person and names the type and what is read', () => {
    const checked = checkedAttachments([{ mediaType: 'text/plain', bytes: Buffer.from('hello') }])
    expect(checked).toEqual({
      ok: false,
      error: 'attachments.0.mediaType: This file is a text file. ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.',
    })
  })

  it('names a kind of file in plain words, with the right article, and never an unknown type', () => {
    const text = (mediaType: string) => {
      const checked = checkedAttachments([{ mediaType, bytes: Buffer.from('x') }])
      return checked.ok ? '' : checked.error
    }
    expect(text('image/heic')).toContain('This file is a HEIC photo.')
    expect(text('image/tiff')).toContain('This file is a TIFF photo.')
    expect(text('application/octet-stream')).toContain('ScadBuddy cannot read this kind of file.')
  })

  it('does not echo a type that is not a plain media type', () => {
    const checked = checkedAttachments([{ mediaType: '<script>x</script>', bytes: Buffer.from('hello') }])
    expect(checked.ok).toBe(false)
    if (!checked.ok) expect(checked.error).not.toContain('<script>')
  })
})

describe('an image a server builds a turn from (no preview, #6267)', () => {
  const bare = { mediaType: 'image/png' as const, bytes: PNG }

  it('passes the check and the turn names the full image only, storing one blob', () => {
    expect(checkedAttachments([bare]).ok).toBe(true)
    const { blobs, sent } = sentImageBlobs([bare])
    expect(blobs).toHaveLength(1)
    expect(sent).toEqual([{ name: blobs[0]!.name, mediaType: 'image/png' }])
  })

  it('still refuses bytes that are not the image type', () => {
    expect(checkedAttachments([{ mediaType: 'image/png', bytes: Buffer.from('nope') }]).ok).toBe(false)
  })
})

describe('a PDF in the session blobs', () => {
  it('carries the invoice file name in its pointer, never the bytes', () => {
    const named = { ...pdf, fileName: 'Bambu order.pdf' }
    const checked = checkedAttachments([named])
    expect(checked.ok && checked.attachments[0]).toMatchObject({ fileName: 'Bambu order.pdf' })
    const { sent } = sentImageBlobs([named])
    expect(sent).toEqual([{ name: sessionImage('application/pdf', PDF).name, mediaType: 'application/pdf', fileName: 'Bambu order.pdf' }])
  })

  it('is named by its sha256 and .pdf, and a sent turn names it without a preview', () => {
    const blob = sessionImage('application/pdf', PDF)
    expect(blob.name).toMatch(BLOB_NAME)
    expect(blob.name.endsWith('.pdf')).toBe(true)
    const { blobs, sent } = sentImageBlobs([pdf])
    expect(blobs).toEqual([blob])
    expect(sent).toEqual([{ name: blob.name, mediaType: 'application/pdf' }])
  })
})
