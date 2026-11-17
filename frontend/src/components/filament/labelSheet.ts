import QRCode from 'qrcode'

/** The label size, in mm (plan 2026-11-17 Decision 12): a 50 x 25 mm roll label. */
export const LABEL_WIDTH_MM = 50
export const LABEL_HEIGHT_MM = 25
export const QR_MM = 21
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

export const qrSvg = (payload: string): Promise<string> => QRCode.toString(payload, { type: 'svg', errorCorrectionLevel: 'M', margin: 0 })
