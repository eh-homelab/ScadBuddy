import QRCode from 'qrcode'
import type { LabelSheet } from '../../api/filament'

/** The default label size, in mm (plan 2026-11-17 Decision 12): a 50 x 25 mm roll label. */
export const LABEL_WIDTH_MM = 50
export const LABEL_HEIGHT_MM = 25

/** #7157 — a label's size in mm, and the sheet it is laid on (`null`: one a page, or packed by the `sheet` layout). */
export interface LabelSize {
  widthMm: number
  heightMm: number
  sheet: LabelSheet | null
}

/** A sheet of labels (`GET /filament/label-templates`): its page and grid, in mm. */
export type { LabelSheet }

export const DEFAULT_SIZE: LabelSize = { widthMm: LABEL_WIDTH_MM, heightMm: LABEL_HEIGHT_MM, sheet: null }

/** A page's width and height in mm (`label_templates.py` `PAGE_MM`). */
export const PAGE_MM = { a4: [210, 297], letter: [215.9, 279.4] } as const

/**
 * The size of a label's big number or code: `cap` mm, smaller when it would overrun the text
 * column beside the QR box (`label_pdf.py` `_big_size`; 22 mm of a 50 mm label's 23.5 mm column).
 */
export function bigTextMm(text: string, cap: number, widthMm: number): number {
  const column = widthMm - 2 * QUIET_MM - QR_MM - 1.5
  return Math.min(cap, (column - 1.5) / (0.62 * Math.max(1, text.length)))
}

/** `items` cut into pages of `perPage` (a sheet's cells). */
export function pagesOf<T>(items: T[], perPage: number): T[][] {
  const pages: T[][] = []
  for (let at = 0; at < items.length; at += perPage) pages.push(items.slice(at, at + perPage))
  return pages
}
/**
 * The QR code's side. Its payload is 52 bytes: version 4, 33 modules, at level M. At 19.8 mm a
 * module is 0.6 mm, and `QUIET_MM` of white on every side is 4 modules (the QR spec's quiet zone;
 * the library's own margin stays 0).
 */
export const QR_MM = 19.8
export const QUIET_MM = 2.6
/** More labels than this in one page is refused: the page loads each spool by its own request. */
export const MAX_LABELS = 100

const PREFIX = 'scadbuddy:spool:'

/** What a label's QR code holds: the spool's id, no host, so a label outlives a move of the app. */
export const labelPayload = (spoolId: string): string => `${PREFIX}${spoolId}`

/** The spool id in a label's text, or null for any other text. */
export function parseLabelPayload(text: string): string | null {
  if (!text.startsWith(PREFIX)) return null
  const id = text.slice(PREFIX.length)
  return id === '' ? null : id
}

/**
 * #6955 — what a place label's QR code holds: the address of its code on ScadBuddy, which a phone camera
 * opens. The base is an https origin of at most 64 characters, so the payload is at most 84 bytes: QR
 * version 5 (37 modules) at level M, and a module of at least 0.5 mm at `QR_MM`.
 */
export const locationPayload = (base: string, code: string): string => `${base.replace(/\/+$/, '')}/x/${code}`

/** The longest payload of a place label: QR version 5 at level M holds 84 bytes. */
export const MAX_PAYLOAD_BYTES = 84

/** The https origin of a candidate address (no path, no trailing slash), or null for an empty, unreadable or non-https one. */
export function httpsOrigin(candidate: string | null | undefined): string | null {
  if (!candidate) return null
  try {
    const url = new URL(candidate)
    return url.protocol === 'https:' ? url.origin : null
  } catch {
    return null
  }
}

/** The address a place label opens: the https origin of the configured base, else this page's own origin. */
export const labelBase = (baseUrl: string | null): string => httpsOrigin(baseUrl) ?? window.location.origin

export const qrSvg = (payload: string): Promise<string> => QRCode.toString(payload, { type: 'svg', errorCorrectionLevel: 'M', margin: 0 })
