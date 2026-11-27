import { API_BASE, request } from './client'
import type { Resolved } from './filament'

/**
 * #8155 — code scans from a photo, decoded on the server. These routes are core's (Go,
 * `tools/go/core/internal/scan`), so the OpenAPI spec and `schema.d.ts` do not have them:
 * the shapes are written here, as the Go package answers them.
 */

/** A photo's blob, as a pointer: never its bytes in JSON (#2479). */
export interface ScanImage {
  key: string
  media_type: string
}

/** Where a scan's photo came from. */
export type ScanSource = 'shutter' | 'nfc' | 'file' | 'app'

/** Where the page was when it took the photo. */
export interface ScanLocation {
  lat: number
  lon: number
  accuracy_m: number | null
  at: string | null
}

/** One code a scan read: its format as BarcodeDetector names it (`qr_code`, `ean_13`), and the thing it names. */
export interface ScannedCode {
  format: string
  text: string
  resolved: Resolved | null
}

export interface CodeScan {
  id: string
  at: string
  source: ScanSource
  actor: string | null
  image: ScanImage
  width: number | null
  height: number | null
  codes: ScannedCode[]
  /** Why nothing could be read: the photo was not an image, or too large to decode. */
  error: 'image_unreadable' | 'image_too_large' | null
  location: ScanLocation | null
  decoded_at: string
}

const BASE = '/scan'

/** Keeps the photo's bytes as a `scan_image` blob. The body is the JPEG itself. */
export const storeScanImage = (photo: Blob) =>
  request<ScanImage & { sha256: string; size: number }>(`${BASE}/images`, {
    method: 'POST',
    body: photo,
    headers: { 'Content-Type': photo.type || 'image/jpeg' },
  })

/** Decodes every code in a stored photo, resolves each, and records the scan. */
export const decodeScan = (body: { key: string; source: ScanSource; location: ScanLocation | null }) =>
  request<CodeScan>(`${BASE}/codes`, { method: 'POST', body: JSON.stringify(body) })

export const listScans = (limit = 20) => request<{ scans: CodeScan[] }>(`${BASE}/scans?limit=${limit}`)

export const decodeScanAgain = (id: string) => request<CodeScan>(`${BASE}/scans/${encodeURIComponent(id)}/decode`, { method: 'POST' })

export const deleteScan = (id: string) => request<void>(`${BASE}/scans/${encodeURIComponent(id)}`, { method: 'DELETE' })

/** The scan's photo, for an `<img>`: the server answers it with its own media type. */
export const scanImageUrl = (id: string) => `${API_BASE}${BASE}/scans/${encodeURIComponent(id)}/image`
