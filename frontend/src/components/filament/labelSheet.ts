import QRCode from 'qrcode'

/** The label size, in mm (plan 2026-11-17 Decision 12): a 50 x 25 mm roll label. */
export const LABEL_WIDTH_MM = 50
export const LABEL_HEIGHT_MM = 25
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

/** The address a place label opens: the configured base, else this page's own origin; no trailing slash. */
export const labelBase = (baseUrl: string | null): string => (baseUrl ?? window.location.origin).replace(/\/+$/, '')

export const qrSvg = (payload: string): Promise<string> => QRCode.toString(payload, { type: 'svg', errorCorrectionLevel: 'M', margin: 0 })
