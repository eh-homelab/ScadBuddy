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

/** The big text's font: Helvetica Bold, as the PDF draws it (`label_pdf.py` `BOLD`), so the two measure alike. */
export const BIG_FONT_FAMILY = 'Helvetica, Arial, sans-serif'

let measuring: CanvasRenderingContext2D | null | undefined

/** The width of `text` in bold at 100 px, or null where no canvas can measure (jsdom, a refused context). */
function boldWidth100(text: string): number | null {
  if (measuring === undefined) {
    try {
      measuring = document.createElement('canvas').getContext('2d')
    } catch {
      measuring = null
    }
  }
  if (!measuring) return null
  measuring.font = `bold 100px ${BIG_FONT_FAMILY}`
  // The span renders with `tabular-nums`: every digit takes the widest digit's width. The canvas has
  // no numeric-variant setting everywhere, so measure the digits that way here.
  const ctx = measuring
  const digits = text.match(/\d/g) ?? []
  const widest = digits.length ? Math.max(...'0123456789'.split('').map((d) => ctx.measureText(d).width)) : 0
  const width = ctx.measureText(text.replace(/\d/g, '')).width + widest * digits.length
  return width > 0 ? width : null
}

/** Forget the cached canvas context (tests that swap `getContext`). */
export const resetMeasuring = (): void => {
  measuring = undefined
}

/**
 * The size of a label's big number or code: `cap` mm, smaller when it would overrun the text
 * column beside the QR box. Like `label_pdf.py` `_big_size`, it measures the text in bold (canvas
 * `measureText`) and takes the size at which it fills the column. Where no canvas can measure, it
 * falls back to a 0.62-per-character estimate (less 1.5 mm), which can differ from the PDF.
 */
export function bigTextMm(text: string, cap: number, widthMm: number): number {
  const column = widthMm - 2 * QUIET_MM - QR_MM - 1.5
  const measured = boldWidth100(text)
  if (measured !== null) return Math.min(cap, (column * 100) / measured)
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
