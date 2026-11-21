import type { SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { z } from 'zod'

// Images the user pastes, drops or attaches in the assistant panel (#1866).
//
// The panel uploads each one as bytes (#1941, #2479: routes/attachments.ts,
// multipart, checked with UserImageSchema below) and its `user.message` names
// them by id (inline images are refused, #1959). An image is the full image for
// the model and a small preview the panel made of it, both bytes: no image is
// base64 inside JSON here (#2479). Both are stored in the session's blobs
// (blobs.ts `sentImageBlobs`) before the `user.turn` event that names them,
// `{name, mediaType, preview: {name, mediaType}}`, and every watcher loads them
// from the session's blob route. The one place the bytes become base64 is
// `userPrompt`: the SDK user message Claude Code turns into the Messages API
// request, which takes an image no other way (Claude Code keeps that message in
// its own transcript, ai_session_entries, which resume reads and no watcher is
// sent). An MCP transcript keeps only
// their count (tools/sessions.ts `condense`). Nothing logs or traces the bytes,
// and a refusal never quotes them.
//
// The types are the four the Messages API reads. The bytes must start with
// their type's signature, so an upload cannot pass off other content as an image.

/** What the Messages API takes as an image. */
export const IMAGE_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'] as const
export type ImageMediaType = (typeof IMAGE_MEDIA_TYPES)[number]

/** What the Messages API takes as a document block: an invoice's PDF (#6267). */
export const DOCUMENT_MEDIA_TYPES = ['application/pdf'] as const
export type DocumentMediaType = (typeof DOCUMENT_MEDIA_TYPES)[number]

/** What a session's blobs keep: an image, or a PDF. */
export type BlobMediaType = ImageMediaType | DocumentMediaType

/** What a preview may be: a still the panel drew. */
export const PREVIEW_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/webp'] as const
export type PreviewMediaType = (typeof PREVIEW_MEDIA_TYPES)[number]

/** Images one message may carry. */
export const IMAGES_MAX = 4
/** One image as the Messages API counts it, in base64 characters: its 5 MB per image. */
export const IMAGE_DATA_MAX = 5 * 1024 * 1024
/** One image's bytes: what IMAGE_DATA_MAX base64 characters hold. */
export const IMAGE_BYTES_MAX = (IMAGE_DATA_MAX / 4) * 3
/** All of a message's images together, in bytes (what 8 MiB of base64 holds). */
export const IMAGES_BYTES_TOTAL_MAX = ((8 * 1024 * 1024) / 4) * 3
/** One PDF's bytes: all that a message's attachments may hold together. */
export const PDF_BYTES_MAX = IMAGES_BYTES_TOTAL_MAX
/** One preview's bytes; the panel draws them at most 256 px on a side. */
export const PREVIEW_BYTES_MAX = 48 * 1024

/** The type `bytes` start as, if it is one of IMAGE_MEDIA_TYPES. */
export function sniff(bytes: Uint8Array): ImageMediaType | undefined {
  const head = Buffer.from(bytes.buffer, bytes.byteOffset, Math.min(bytes.byteLength, 16))
  const ascii = (from: number, text: string) => head.subarray(from, from + text.length).toString('latin1') === text
  if (head.length >= 8 && head.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return 'image/png'
  }
  if (head.length >= 3 && head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return 'image/jpeg'
  if (ascii(0, 'GIF87a') || ascii(0, 'GIF89a')) return 'image/gif'
  if (ascii(0, 'RIFF') && ascii(8, 'WEBP')) return 'image/webp'
  return undefined
}

/** Whether `bytes` start as a PDF: `%PDF-`. */
export function sniffPdf(bytes: Uint8Array): boolean {
  return Buffer.from(bytes.buffer, bytes.byteOffset, Math.min(bytes.byteLength, 5)).toString('latin1') === '%PDF-'
}

function stored<T extends readonly [string, ...string[]]>(types: T, max: number) {
  return z
    .object({
      mediaType: z.enum(types),
      // Messages name the field only: a refusal never quotes the bytes.
      bytes: z
        .instanceof(Buffer, { message: 'not bytes' })
        .refine((bytes) => bytes.length > 0, { message: 'empty' })
        .refine((bytes) => bytes.length <= max, { message: `at most ${max} bytes` }),
    })
    .refine((image) => sniff(image.bytes) === image.mediaType, {
      message: 'the bytes are not the image type it names',
      path: ['bytes'],
    })
}

/** An image's small copy, as bytes. */
export type ImagePreview = { mediaType: PreviewMediaType; bytes: Buffer }
export const ImagePreviewSchema = stored(PREVIEW_MEDIA_TYPES, PREVIEW_BYTES_MAX)

/** An image the user sends, and its preview, as bytes: what the panel uploads. */
export type UserImage = { mediaType: ImageMediaType; bytes: Buffer; preview: ImagePreview }
export const UserImageSchema = stored(IMAGE_MEDIA_TYPES, IMAGE_BYTES_MAX).and(z.object({ preview: ImagePreviewSchema }))

/**
 * An image a server builds a turn from (#6267: an invoice image the API read from a file).
 * The agent has no image library to draw a preview with (the panel draws the panel's own),
 * so this one has none, and its turn names the full image only: the panel shows that.
 */
export type ServerImage = { mediaType: ImageMediaType; bytes: Buffer; preview?: ImagePreview }
const ServerImageSchema = stored(IMAGE_MEDIA_TYPES, IMAGE_BYTES_MAX).and(z.object({ preview: ImagePreviewSchema.optional() }))

/** A PDF the user sends, as bytes. It has no preview: the panel draws none. */
export type UserDocument = { mediaType: DocumentMediaType; bytes: Buffer; fileName?: string }
export const UserDocumentSchema = z
  .object({
    mediaType: z.enum(DOCUMENT_MEDIA_TYPES),
    // The file's own name, for the panel's chip (#6267): a name the person gave it, never the bytes. The cap matches the backend's 255.
    fileName: z.string().min(1).max(255).optional(),
    bytes: z
      .instanceof(Buffer, { message: 'not bytes' })
      .refine((bytes) => bytes.length > 0, { message: 'empty' })
      .refine((bytes) => bytes.length <= PDF_BYTES_MAX, { message: `at most ${PDF_BYTES_MAX} bytes` }),
  })
  .refine((doc) => sniffPdf(doc.bytes), { message: 'the bytes are not a PDF', path: ['bytes'] })

/** What a message carries for the model: an image, or a PDF. */
export type UserAttachment = ServerImage | UserDocument

export const UserImagesSchema = z
  .array(UserImageSchema)
  .min(1)
  .max(IMAGES_MAX)
  .refine((images) => images.reduce((n, i) => n + i.bytes.length, 0) <= IMAGES_BYTES_TOTAL_MAX, {
    message: `the images come to more than ${IMAGES_BYTES_TOTAL_MAX} bytes together`,
  })

/** The checked images, typed as the store and the turn take them. */
export function checkedImages(images: unknown): { ok: true; images: UserImage[] } | { ok: false; error: string } {
  const parsed = UserImagesSchema.safeParse(images)
  if (parsed.success) return { ok: true, images: parsed.data as UserImage[] }
  // Messages name fields and caps only: they never quote the bytes.
  return { ok: false, error: parsed.error.issues.map((i) => `${i.path.join('.') || 'images'}: ${i.message}`).join('; ') }
}

/** Plain names for the kinds of file people upload that ScadBuddy cannot read. */
const KINDS: Record<string, string> = {
  'image/heic': 'a HEIC photo',
  'image/heif': 'a HEIF photo',
  'image/tiff': 'a TIFF photo',
  'image/bmp': 'a BMP photo',
  'image/svg+xml': 'an SVG drawing',
  'text/plain': 'a text file',
  'text/html': 'a web page',
  'text/csv': 'a spreadsheet file',
  'application/zip': 'a ZIP file',
}

/** A refusal for a file type ScadBuddy cannot read, worded for the person. */
export function unsupported(mediaType: unknown): string {
  const type = typeof mediaType === 'string' ? mediaType.toLowerCase() : ''
  const kind = KINDS[type] ?? (type.startsWith('text/') ? 'a text file' : undefined)
  const reads = 'ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.'
  return kind ? `This file is ${kind}. ${reads}` : `ScadBuddy cannot read this kind of file. It ${reads.slice('ScadBuddy '.length)}`
}

/**
 * The checked attachments of a message that may carry a PDF (#6267): images, and PDFs. Any
 * other type (text, HEIC) is refused; a message names fields and caps, never the bytes.
 */
export function checkedAttachments(
  attachments: unknown,
): { ok: true; attachments: UserAttachment[] } | { ok: false; error: string } {
  if (!Array.isArray(attachments) || attachments.length < 1 || attachments.length > IMAGES_MAX) {
    return { ok: false, error: `attachments: between 1 and ${IMAGES_MAX} are required` }
  }
  const types: readonly unknown[] = [...IMAGE_MEDIA_TYPES, ...DOCUMENT_MEDIA_TYPES]
  const problems: string[] = []
  const checked: UserAttachment[] = []
  for (const [index, attachment] of attachments.entries()) {
    const mediaType = (attachment as { mediaType?: unknown } | null)?.mediaType
    if (!types.includes(mediaType)) {
      problems.push(`attachments.${index}.mediaType: ${unsupported(mediaType)}`)
      continue
    }
    const parsed = (mediaType === 'application/pdf' ? UserDocumentSchema : ServerImageSchema).safeParse(attachment)
    if (parsed.success) checked.push(parsed.data as UserAttachment)
    else problems.push(...parsed.error.issues.map((i) => `attachments.${index}.${i.path.join('.') || 'attachment'}: ${i.message}`))
  }
  if (problems.length) return { ok: false, error: problems.join('; ') }
  if (checked.reduce((n, a) => n + a.bytes.length, 0) > IMAGES_BYTES_TOTAL_MAX) {
    return { ok: false, error: `attachments: more than ${IMAGES_BYTES_TOTAL_MAX} bytes together` }
  }
  return { ok: true, attachments: checked }
}

/**
 * The turn's prompt as one SDK user message: the images first, as the Messages
 * API advises, then the text (the user's words and the page context). Each
 * iteration starts afresh, since fallback.ts may run a query again as it was.
 *
 * The SDK boundary (#2479): the SDK takes an image in a user message only as a
 * base64 source, which Claude Code puts in the Messages API request.
 */
export function userPrompt(text: string, images: readonly Pick<UserAttachment, 'mediaType' | 'bytes'>[]): AsyncIterable<SDKUserMessage> {
  const message: SDKUserMessage = {
    type: 'user',
    session_id: '',
    parent_tool_use_id: null,
    message: {
      role: 'user',
      content: [
        ...images.map((i) =>
          i.mediaType === 'application/pdf'
            ? {
                type: 'document' as const,
                source: { type: 'base64' as const, media_type: i.mediaType, data: i.bytes.toString('base64') },
              }
            : {
                type: 'image' as const,
                source: { type: 'base64' as const, media_type: i.mediaType, data: i.bytes.toString('base64') },
              },
        ),
        { type: 'text' as const, text },
      ],
    },
  }
  return {
    [Symbol.asyncIterator]: async function* () {
      yield message
    },
  }
}
