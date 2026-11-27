import { createContext, useContext } from 'react'
import type { CodeScan, ScanLocation, ScanSource } from '../../../api/scan'

/**
 * #8155 — the Scan page's camera: a live preview of the rear camera, and a still of it for
 * each scan. A shutter tap takes one (`capture('shutter')`), and so may an NFC read
 * (`capture('nfc')`), to keep the read's photo and to read any label in view. Each still
 * is a JPEG (a canvas's `toBlob`, never base64), stored as a `scan_image` blob, decoded on
 * the server and recorded with where the phone was, when the page may know it.
 */
export interface CapturedScan {
  /** The photo's blob key, which a tag read can name as `image: {key, media_type}`. */
  key: string
  mediaType: string
  scan: CodeScan
}

export type CameraState = 'off' | 'starting' | 'on' | 'unavailable'

export interface ScanCamera {
  state: CameraState
  /** Why the camera is unavailable, in words for the person. */
  reason: string | null
  start: () => Promise<void>
  stop: () => void
  /** The live preview's element: the stills are taken from it. */
  bindVideo: (video: HTMLVideoElement | null) => void
  /** A still of the preview, scanned; null while the camera is off. A failure rejects. */
  capture: (source?: ScanSource) => Promise<CapturedScan | null>
  /** A photo the person chose, drawn again as a JPEG (so a HEIC the browser can show is read too), scanned. */
  scanFile: (file: File) => Promise<CapturedScan>
  /** This page's scans, newest first. */
  scans: CodeScan[]
  /** A scan read again or deleted, in the list. */
  replace: (scan: CodeScan) => void
  forget: (id: string) => void
}

export const ScanCameraContext = createContext<ScanCamera | null>(null)

/** The page's camera; null outside `ScanCameraProvider`. */
export function useScanCamera(): ScanCamera | null {
  return useContext(ScanCameraContext)
}

/** The longest side of a still from the preview: a code reads well at this size, and the upload stays small. */
export const PREVIEW_STILL_MAX = 1920
/** The longest side of a chosen photo, drawn again. */
export const FILE_STILL_MAX = 2560
/** How long a scan waits for the phone's place; then it is sent without one. */
export const LOCATION_WAIT_MS = 3000

/** `source` drawn as a JPEG no larger than `max` on its longest side. */
export function stillOf(source: CanvasImageSource, width: number, height: number, max: number): Promise<Blob> {
  const scale = Math.min(1, max / Math.max(width, height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(width * scale))
  canvas.height = Math.max(1, Math.round(height * scale))
  const context = canvas.getContext('2d')
  if (!context) return Promise.reject(new Error('This browser cannot draw the photo.'))
  context.drawImage(source, 0, 0, canvas.width, canvas.height)
  return new Promise((resolve, reject) =>
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('This browser cannot make a JPEG of the photo.'))), 'image/jpeg', 0.9),
  )
}

/**
 * Where the phone is, from one GPS fix: null when the browser has no location, the person
 * refused it, or no fix came within `wait`. It never makes a scan wait longer than that.
 */
export function whereNow(wait = LOCATION_WAIT_MS): Promise<ScanLocation | null> {
  const geo = typeof navigator === 'undefined' ? undefined : navigator.geolocation
  if (!geo) return Promise.resolve(null)
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), wait)
    try {
      geo.getCurrentPosition(
        (position) => {
          clearTimeout(timer)
          resolve({
            lat: position.coords.latitude,
            lon: position.coords.longitude,
            accuracy_m: Number.isFinite(position.coords.accuracy) ? position.coords.accuracy : null,
            at: new Date(position.timestamp).toISOString(),
          })
        },
        () => {
          clearTimeout(timer)
          resolve(null)
        },
        { enableHighAccuracy: true, timeout: wait, maximumAge: 30_000 },
      )
    } catch {
      clearTimeout(timer)
      resolve(null)
    }
  })
}

/** Why the browser gave no camera, in words for the person. */
export function cameraRefusal(cause: unknown): string {
  if (cause instanceof DOMException && cause.name === 'NotAllowedError') return 'The camera is not allowed for this page. Choose a photo instead, or allow the camera in the browser’s site settings.'
  if (cause instanceof DOMException && (cause.name === 'NotFoundError' || cause.name === 'OverconstrainedError')) return 'This device has no camera the page can use. Choose a photo instead.'
  return 'The camera did not start. Choose a photo instead.'
}

/** The names of the code formats, as a person reads them. */
const FORMAT_NAMES: Record<string, string> = {
  qr_code: 'QR code',
  data_matrix: 'Data Matrix',
  ean_13: 'EAN-13',
  ean_8: 'EAN-8',
  upc_a: 'UPC-A',
  upc_e: 'UPC-E',
  code_128: 'Code 128',
}

export const formatName = (format: string) => FORMAT_NAMES[format] ?? format
