import { useCallback, useRef } from 'react'
import { ApiError, api } from '../api/client'
import type { ModelSummary } from '../api/types'

/** #279 — what a template's media may be; the server checks the bytes again (#274). */
export const MEDIA_ACCEPT = 'image/png,image/jpeg,image/webp,video/mp4,video/webm'

const MiB = 1024 * 1024
/** The server's `MAX_IMAGE_BYTES`: images are committed to the template's history. */
export const MAX_MEDIA_IMAGE_BYTES = 10 * MiB

const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])
const VIDEO_TYPES = new Set(['video/mp4', 'video/webm'])

/** Whether a file is an image or a video, from its type or else its name. */
export function mediaKindOf(file: File): 'image' | 'video' | undefined {
  if (IMAGE_TYPES.has(file.type) || /\.(png|jpe?g|webp)$/i.test(file.name)) return 'image'
  if (VIDEO_TYPES.has(file.type) || /\.(mp4|webm)$/i.test(file.name)) return 'video'
  return undefined
}

/**
 * The images and videos a paste carries, as files to add like an upload (#722).
 * Anything `image/*` or `video/*` is taken, so a GIF is refused by `mediaProblem`
 * with a reason rather than dropped without a word. A pasted screenshot arrives as
 * `image.png` (or with no name); it gets a name of its own, so several are told apart.
 */
export function pastedMedia(data: DataTransfer | null): File[] {
  if (!data) return []
  const isMedia = (type: string) => type.startsWith('image/') || type.startsWith('video/')
  let files = Array.from(data.files ?? []).filter((file) => isMedia(file.type))
  if (files.length === 0) {
    files = Array.from(data.items ?? [])
      .filter((item) => item.kind === 'file' && isMedia(item.type))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
  }
  const when = fileStamp()
  return files.map((file, index) => {
    if (file.name && !/^image\.\w+$/i.test(file.name)) return file
    const extension = file.type.split('/')[1]?.replace(/[^a-z0-9]/gi, '') || 'bin'
    const suffix = files.length > 1 ? `-${index + 1}` : ''
    return new File([file], `pasted-${when}${suffix}.${extension}`, { type: file.type })
  })
}

/** A sortable, file-name-safe local time: 20260929-011530. */
export function fileStamp(now = new Date()): string {
  const two = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}${two(now.getMonth() + 1)}${two(now.getDate())}-${two(now.getHours())}${two(now.getMinutes())}${two(now.getSeconds())}`
}

/**
 * Whether `element` takes typed or pasted text, where a paste is the field's own.
 * A checkbox or a button does not.
 */
export function takesText(element: Element | null): boolean {
  if (!element) return false
  if (element instanceof HTMLTextAreaElement) return true
  if (element instanceof HTMLInputElement) {
    return !['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'].includes(
      element.type,
    )
  }
  return element instanceof HTMLElement && element.isContentEditable
}

/** Megabytes as the server's 413 names them (`limit / MiB`). */
export function formatMegabytes(bytes: number): string {
  return `${Number((bytes / MiB).toFixed(2))} MB`
}

/**
 * Why a file cannot be added to a template's media, or null when it can. Checked
 * before anything is sent, so a gigabyte over the limit never starts uploading.
 */
export function mediaProblem(file: File, limit: number): string | null {
  const kind = mediaKindOf(file)
  if (!kind) return `${file.name} is not a PNG, JPEG or WebP image, or an MP4 or WebM video.`
  if (file.size > limit) {
    return `${file.name} is larger than the ${formatMegabytes(limit)} upload limit.`
  }
  if (kind === 'image' && file.size > MAX_MEDIA_IMAGE_BYTES) {
    return `${file.name} is larger than the ${formatMegabytes(MAX_MEDIA_IMAGE_BYTES)} limit for images.`
  }
  return null
}

/**
 * The deployment's upload limit from `GET /settings`, read once per component. When
 * the settings cannot be read the server's own 413 is the only check left.
 */
export function useUploadLimit(): () => Promise<number> {
  const limit = useRef<Promise<number> | null>(null)
  return useCallback(() => {
    limit.current ??= api
      .getSettings()
      .then((settings) => settings.media_upload_max_bytes)
      .catch(() => Number.POSITIVE_INFINITY)
    return limit.current
  }, [])
}

/** What to tell a person about a failed call. */
export function failure(caught: unknown): string {
  if (caught instanceof ApiError) return caught.detail
  return caught instanceof Error ? caught.message : String(caught)
}

/** The upload store's cap for a `// file` parameter (`assets.py`). */
export const MAX_ASSET_BYTES = 8 * MiB
/** Longest edge a media image is scaled to before it becomes a parameter's PNG. */
const ASSET_EDGE = 1024

/**
 * A template image (PNG, JPEG or WebP) as a PNG a `// file` parameter can take. A
 * PNG under the upload cap goes as it is; anything else is redrawn as a PNG no
 * larger than `ASSET_EDGE` on its long side (the server shrinks it further anyway).
 */
export async function imageAsPng(blob: Blob, name: string): Promise<File> {
  const base = name.replace(/\.[^.]+$/, '') || 'image'
  if (blob.type === 'image/png' && blob.size <= MAX_ASSET_BYTES) {
    // From the bytes, not the Blob: a Blob from another realm (a test's fetch) is not
    // one this realm's File takes as data.
    return new File([await blob.arrayBuffer()], `${base}.png`, { type: 'image/png' })
  }
  const bitmap = await createImageBitmap(blob)
  const scale = Math.min(1, ASSET_EDGE / Math.max(bitmap.width, bitmap.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(bitmap.width * scale))
  canvas.height = Math.max(1, Math.round(bitmap.height * scale))
  canvas.getContext('2d')?.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  bitmap.close()
  const png = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
  if (!png) throw new Error(`Could not convert ${name} to a PNG.`)
  return new File([png], `${base}.png`, { type: 'image/png' })
}

/**
 * Uploads `file` as the template's last media item and answers with the record and
 * the new item's id. The id is the one the answer has that `before` did not.
 */
export async function addMedia(
  slug: string,
  file: File,
  before: MediaItemIds,
): Promise<{ model: ModelSummary; id: string | undefined }> {
  const known = new Set(before.map((item) => item.id))
  const model = await api.uploadMedia(slug, file)
  const added = (model.media ?? []).filter((item) => !known.has(item.id))
  return { model, id: added[added.length - 1]?.id }
}

/** Moves `id` to the front of the template's media, which makes it the cover. */
export async function makeCover(slug: string, media: MediaItemIds, id: string): Promise<ModelSummary | null> {
  const ids = media.map((item) => item.id)
  if (ids[0] === id || !ids.includes(id)) return null
  return await api.reorderMedia(slug, [id, ...ids.filter((other) => other !== id)])
}

type MediaItemIds = { id: string }[]
