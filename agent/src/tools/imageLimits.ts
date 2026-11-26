import { z } from 'zod'
import { IMAGE_DATA_MAX } from '../sessions/images.js'

// The inline limit for images a tool shows the model (#7745): `ai_settings` key
// `tool_image_inline_max_bytes`, set in Settings → Assistant → Images
// (routes/imageSettings.ts). An image over it goes to the workspace as a pointer
// (tools/imageResult.ts). The largest value is what still fits `IMAGE_DATA_MAX`
// as base64; the smallest keeps a small thumbnail inline.

export const SETTING_TOOL_IMAGE_INLINE_MAX = 'tool_image_inline_max_bytes'
export const DEFAULT_TOOL_IMAGE_INLINE_MAX = 512 * 1024
export const MIN_TOOL_IMAGE_INLINE_MAX = 16 * 1024
export const MAX_TOOL_IMAGE_INLINE_MAX = Math.floor(IMAGE_DATA_MAX / 4) * 3
/** The most of an image a tool reads before it answers that it is too large to keep. */
export const IMAGE_READ_MAX = 25 * 1024 * 1024

export const ToolImageInlineMax = z.number().int().min(MIN_TOOL_IMAGE_INLINE_MAX).max(MAX_TOOL_IMAGE_INLINE_MAX)

/** The stored limit, or the default for anything a PUT could not have written. */
export function toolImageInlineMax(stored: unknown): number {
  const parsed = ToolImageInlineMax.safeParse(stored)
  return parsed.success ? parsed.data : DEFAULT_TOOL_IMAGE_INLINE_MAX
}
