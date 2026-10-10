import { IMAGE_MEDIA_TYPES, type ImagePreview, type UserImage } from './protocol'

/**
 * #1866 — images the user pastes, drops or attaches in the assistant's composer, made
 * ready to send: the image for the model, scaled down when it is larger than the model
 * reads, and a small JPEG preview for the transcript. The caps mirror the agent's
 * (`agent/src/sessions/images.ts`), which checks them again with the bytes.
 */

/** Images one message may carry (agent `IMAGES_MAX`). */
export const IMAGES_MAX = 4
/** One image's base64 (agent `IMAGE_DATA_MAX`: the Messages API's 5 MB). */
export const IMAGE_DATA_MAX = 5 * 1024 * 1024
/** All of a message's images together, as base64 (agent `IMAGES_DATA_TOTAL_MAX`). */
export const IMAGES_DATA_TOTAL_MAX = 8 * 1024 * 1024
/** A preview's base64 (agent `PREVIEW_DATA_MAX`). */
const PREVIEW_DATA_MAX = 64 * 1024

/**
 * The long edge an image is scaled to when the agent's setting cannot be read (agent
 * `routes/imageSettings.ts` DEFAULT_IMAGE_LONG_EDGE, `GET /api/v1/ai/settings/images`):
 * the Messages API's standard-tier edge, which models before Claude 4.7 scale to anyway.
 */
export const DEFAULT_IMAGE_EDGE = 1568
/** The long edge of a preview in the transcript. */
export const PREVIEW_EDGE = 256

export const IMAGE_ACCEPT = IMAGE_MEDIA_TYPES.join(',')

type MediaType = UserImage['mediaType']

/** Decodes an image and draws it, scaled, as another. The browser's is `canvasCodec`. */
export interface ImageCodec {
  open(blob: Blob): Promise<{
    width: number
    height: number
    /** The image drawn at `width` × `height` and encoded as `type` (a JPEG on white). */
    encode(width: number, height: number, type: string, quality?: number): Promise<Blob>
    close(): void
  }>
}

export const canvasCodec: ImageCodec = {
  async open(blob) {
    const bitmap = await createImageBitmap(blob)
    return {
      width: bitmap.width,
      height: bitmap.height,
      async encode(width, height, type, quality) {
        const canvas = document.createElement('canvas')
        canvas.width = width
        canvas.height = height
        const context = canvas.getContext('2d')
        if (!context) throw new Error('no 2D canvas')
        if (type === 'image/jpeg') {
          // JPEG has no transparency: what was clear shows white, as on the page, not black.
          context.fillStyle = '#fff'
          context.fillRect(0, 0, width, height)
        }
        context.drawImage(bitmap, 0, 0, width, height)
        const out = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, type, quality))
        if (!out) throw new Error('the canvas made no image')
        return out
      },
      close: () => bitmap.close(),
    }
  },
}

function isMediaType(type: string): type is MediaType {
  return (IMAGE_MEDIA_TYPES as readonly string[]).includes(type)
}

/** The images a paste, drop or file pick carries; anything else (text, video) is left alone. */
export function composerImages(data: Pick<DataTransfer, 'files' | 'items'> | null): File[] {
  if (!data) return []
  const isImage = (type: string) => type.startsWith('image/')
  const files = Array.from(data.files ?? []).filter((file) => isImage(file.type))
  if (files.length > 0) return files
  return Array.from(data.items ?? [])
    .filter((item) => item.kind === 'file' && isImage(item.type))
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null)
}

async function base64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  let binary = ''
  // In chunks: one String.fromCharCode call per byte array overflows the stack.
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  }
  return btoa(binary)
}

const encodedLength = (bytes: number) => Math.ceil(bytes / 3) * 4

function fit(width: number, height: number, edge: number): [number, number] {
  const scale = Math.min(1, edge / Math.max(width, height))
  return [Math.max(1, Math.round(width * scale)), Math.max(1, Math.round(height * scale))]
}

/**
 * `file` as the composer sends it, or an error that says why it cannot be. Images
 * within `edge` (the stored setting) and IMAGE_DATA_MAX go as they are (an animated GIF
 * stays animated); larger ones are drawn at `edge` in their own type, or as a JPEG when
 * that is still too large.
 */
export async function prepareImage(
  file: File,
  codec: ImageCodec = canvasCodec,
  edge: number = DEFAULT_IMAGE_EDGE,
): Promise<UserImage> {
  const type = file.type
  if (!isMediaType(type)) throw new Error(`${file.name} is not a PNG, JPEG, GIF or WebP image.`)
  let image: Awaited<ReturnType<ImageCodec['open']>>
  try {
    image = await codec.open(file)
  } catch {
    throw new Error(`${file.name} could not be read as an image.`)
  }
  try {
    let full: { mediaType: MediaType; blob: Blob } = { mediaType: type, blob: file }
    if (Math.max(image.width, image.height) > edge || encodedLength(file.size) > IMAGE_DATA_MAX) {
      const [width, height] = fit(image.width, image.height, edge)
      // A GIF is drawn as a PNG: canvases do not encode GIFs.
      const own = type === 'image/gif' ? 'image/png' : type
      let blob = await image.encode(width, height, own, 0.9)
      if (encodedLength(blob.size) > IMAGE_DATA_MAX || !isMediaType(blob.type)) {
        blob = await image.encode(width, height, 'image/jpeg', 0.85)
      }
      if (encodedLength(blob.size) > IMAGE_DATA_MAX || !isMediaType(blob.type)) {
        throw new Error(`${file.name} is too large to send, even scaled down.`)
      }
      full = { mediaType: blob.type as MediaType, blob }
    }
    return {
      mediaType: full.mediaType,
      data: await base64(full.blob),
      preview: await preview(image, file.name),
    }
  } finally {
    image.close()
  }
}

async function preview(image: Awaited<ReturnType<ImageCodec['open']>>, name: string): Promise<ImagePreview> {
  for (const edge of [PREVIEW_EDGE, PREVIEW_EDGE / 2]) {
    const [width, height] = fit(image.width, image.height, edge)
    const blob = await image.encode(width, height, 'image/jpeg', 0.8)
    if (blob.type === 'image/jpeg' && encodedLength(blob.size) <= PREVIEW_DATA_MAX) {
      return { mediaType: 'image/jpeg', data: await base64(blob) }
    }
  }
  throw new Error(`${name} could not be made into a preview.`)
}

/** A preview or image as an `<img>` source. */
export function dataUrl(image: { mediaType: string; data: string }): string {
  return `data:${image.mediaType};base64,${image.data}`
}
