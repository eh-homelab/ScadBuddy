import { useCallback, useRef } from 'react'
import { ApiError, api } from '../api/client'

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
