import type { SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { z } from 'zod'

// Images the user pastes, drops or attaches in the assistant panel (#1866).
//
// The panel uploads each one (#1941, routes/attachments.ts, checked with
// UserImageSchema below) and its `user.message` names them by id (inline images
// are refused, #1959). An image is the full image, base64, for the model, and a
// small preview the panel made of it. The model gets the full images as `image` content blocks in the turn's user message
// (`userPrompt`), so they are kept only where Claude Code keeps the turn: the
// SDK transcript (ai_session_entries), which resume reads and no watcher is sent.
// The `user.turn` event, which the event log keeps and every watcher replays,
// carries the previews only (`previewsOf`), and an MCP transcript only their
// count (tools/sessions.ts `condense`). Nothing logs or traces the bytes, and a
// refusal never quotes them.
//
// The types are the four the Messages API reads. The bytes must start with
// their type's signature, so an upload cannot pass off other content as an image.

/** What the Messages API takes as an image. */
export const IMAGE_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'] as const
export type ImageMediaType = (typeof IMAGE_MEDIA_TYPES)[number]

/** What a preview may be: a still the panel drew. */
export const PREVIEW_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/webp'] as const

/** Images one message may carry. */
export const IMAGES_MAX = 4
/** One image's base64 (characters): the Messages API's 5 MB per image. */
export const IMAGE_DATA_MAX = 5 * 1024 * 1024
/** All of a message's images together (base64 characters). */
export const IMAGES_DATA_TOTAL_MAX = 8 * 1024 * 1024
/** One preview's base64 (characters); the panel draws them at most 256 px on a side. */
export const PREVIEW_DATA_MAX = 64 * 1024

// A plain class, not a group repeated per quartet: V8 recurses on those and
// runs out of stack on a few megabytes. The length is checked apart.
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/

/** The type `data`'s first bytes say it is, if it is one of IMAGE_MEDIA_TYPES. */
export function sniff(data: string): ImageMediaType | undefined {
  const head = Buffer.from(data.slice(0, 24), 'base64')
  const ascii = (from: number, text: string) => head.subarray(from, from + text.length).toString('latin1') === text
  if (head.length >= 8 && head.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return 'image/png'
  }
  if (head.length >= 3 && head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return 'image/jpeg'
  if (ascii(0, 'GIF87a') || ascii(0, 'GIF89a')) return 'image/gif'
  if (ascii(0, 'RIFF') && ascii(8, 'WEBP')) return 'image/webp'
  return undefined
}

function encoded<T extends readonly [string, ...string[]]>(types: T, max: number) {
  return z
    .object({
      mediaType: z.enum(types),
      // Messages name the field only: a refusal never quotes the bytes.
      data: z
        .string()
        .min(1)
        .max(max, { message: `at most ${max} base64 characters` })
        .refine((data) => data.length % 4 === 0 && BASE64.test(data), { message: 'not base64' }),
    })
    .refine((image) => sniff(image.data) === image.mediaType, {
      message: 'the bytes are not the image type it names',
      path: ['data'],
    })
}

export const ImagePreviewSchema = encoded(PREVIEW_MEDIA_TYPES, PREVIEW_DATA_MAX)
export type ImagePreview = z.infer<typeof ImagePreviewSchema>

export const UserImageSchema = encoded(IMAGE_MEDIA_TYPES, IMAGE_DATA_MAX).and(
  z.object({ preview: ImagePreviewSchema }),
)
export type UserImage = z.infer<typeof UserImageSchema>

export const UserImagesSchema = z
  .array(UserImageSchema)
  .min(1)
  .max(IMAGES_MAX)
  .refine((images) => images.reduce((n, i) => n + i.data.length, 0) <= IMAGES_DATA_TOTAL_MAX, {
    message: `the images come to more than ${IMAGES_DATA_TOTAL_MAX} base64 characters together`,
  })

/** What the `user.turn` event keeps of a message's images. */
export function previewsOf(images: readonly UserImage[]): ImagePreview[] {
  return images.map(({ preview }) => ({ mediaType: preview.mediaType, data: preview.data }))
}

/**
 * The turn's prompt as one SDK user message: the images first, as the Messages
 * API advises, then the text (the user's words and the page context). Each
 * iteration starts afresh, since fallback.ts may run a query again as it was.
 */
export function userPrompt(text: string, images: readonly UserImage[]): AsyncIterable<SDKUserMessage> {
  const message: SDKUserMessage = {
    type: 'user',
    session_id: '',
    parent_tool_use_id: null,
    message: {
      role: 'user',
      content: [
        ...images.map((i) => ({
          type: 'image' as const,
          source: { type: 'base64' as const, media_type: i.mediaType, data: i.data },
        })),
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
