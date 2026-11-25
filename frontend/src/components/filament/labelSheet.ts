import QRCode from 'qrcode'
import type { LabelOutput, LabelSheet, SpoolDetail } from '../../api/filament'

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
 * The browser label's big text font: Helvetica Bold. The PDF draws DejaVu Sans Bold since #7216
 * (`label_fonts.py`, R-7216-1: the browser print keeps its own fonts), which is wider, so the
 * PDF's big text can be a little smaller than the browser's.
 */
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
export function bigTextMm(text: string, cap: number, widthMm: number, qr = true): number {
  const column = textColumnMm(widthMm, qr)
  const measured = boldWidth100(text)
  if (measured !== null) return Math.min(cap, (column * 100) / measured)
  return Math.min(cap, (column - 1.5) / (0.62 * Math.max(1, text.length)))
}

/**
 * The text column's right padding, and the room kept above and below its lines when they are
 * fitted to the label's height (`label_pdf.py` `PAD_MM`, R-7216-10).
 */
export const PAD_MM = 1.5

/** The text column's width: beside the QR box, or from the quiet zone when the label has no QR code (R-7216-10). */
export const textColumnMm = (widthMm: number, qr: boolean): number => (qr ? widthMm - 2 * QUIET_MM - QR_MM - PAD_MM : widthMm - QUIET_MM - PAD_MM)

/** #7216 — a field a spool label can print (R-7216-9). */
export type LabelField = NonNullable<LabelOutput['fields']>[number]

/** Every field, in the order the lines print (`label_output.py` `ALL_FIELDS`). */
export const ALL_FIELDS: readonly LabelField[] = ['number', 'material', 'colour', 'hex', 'brand', 'location', 'temps', 'weight', 'note', 'added', 'qr']

/** What spool labels printed before #7216 (`label_output.py` `DEFAULT_FIELDS`). */
export const DEFAULT_FIELDS: readonly LabelField[] = ['number', 'material', 'colour', 'brand', 'qr']

/**
 * The line sizes in mm, as `label_pdf.py`'s, which must stay the same so the browser keeps the
 * same lines as the PDF: the big number (`NUMBER_MM`, a line height of its size), the first line
 * under it (`GAP_MM` above it), and each other line (`LEADING` times its size, `FIELD_MM`).
 */
export const NUMBER_MM = 9
const GAP_MM = 1
const LEADING = 1.25
const FIELD_MM: Partial<Record<LabelField, number>> = { material: 3.2, colour: 3, hex: 2.6, brand: 2.6, location: 2.6, temps: 2.6, weight: 2.6, note: 2.6, added: 2.6 }
/** The fields that print in bold (`label_pdf.py` `BOLD_FIELDS`). */
const BOLD_FIELDS: ReadonlySet<LabelField> = new Set(['material'])

/** One line of a spool label: its field, its text, its size in mm, and its weight. */
export type LabelLine = { field: LabelField; text: string; sizeMm: number; bold: boolean }

/** The nozzle temperatures as the label prints them (`labels.py` `temps_text`): `190–230 °C`, or one value. */
function tempsText(low: number | null | undefined, high: number | null | undefined): string | null {
  if (low == null || high == null || low === high) {
    const one = low ?? high
    return one == null ? null : `${one} °C`
  }
  return `${low}–${high} °C`
}

/** The day `iso` falls on in UTC, as `YYYY-MM-DD`, or null for no date. */
function utcDay(iso: string | null | undefined): string | null {
  if (!iso) return null
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? null : at.toISOString().slice(0, 10)
}

/** Python's `str.splitlines` breaks. */
// eslint-disable-next-line no-control-regex -- \x1c to \x1e break a line in Python too.
const LINE_BREAK = /\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/
// The characters where Python's `str.isspace()` is true, which `str.strip()` removes. JavaScript's
// `trim()` differs: it keeps \x1c to \x1f and \x85, and it removes \ufeff.
const PY_SPACE = '[\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]'
const PY_STRIP = new RegExp(`^${PY_SPACE}+|${PY_SPACE}+$`, 'g')

/** The note's first line, as `labels.py` `pdf_labels` makes it: `strip()`, `splitlines()`, then `[0].strip()`. */
export function noteLine(note: string | null | undefined): string | null {
  return (note ?? '').replace(PY_STRIP, '').split(LINE_BREAK)[0]?.replace(PY_STRIP, '') || null
}

/**
 * R-7216-18: the fields with one box changed, in `ALL_FIELDS` order. A label prints at least one
 * field, so a change that would leave no field is not applied.
 */
export function tickField(fields: readonly LabelField[], field: LabelField, on: boolean): LabelField[] {
  const next = ALL_FIELDS.filter((one) => (one === field ? on : fields.includes(one)))
  return next.length > 0 ? next : [...fields]
}

/** Each field's text, as `labels.py` `pdf_labels` makes it; null makes no line. */
function fieldText(spool: SpoolDetail, field: LabelField): string | null {
  const effective = spool.effective
  switch (field) {
    case 'number':
      return `#${spool.number}`
    case 'material':
      return [spool.names?.material, spool.names?.subtype].filter(Boolean).join(' ') || null
    case 'colour':
      return effective?.colour_name || spool.colour_name || null
    case 'hex':
      return effective?.rgba ? effective.rgba.slice(0, 7).toUpperCase() : null
    case 'brand':
      return spool.names?.brand || null
    case 'location':
      return spool.names?.location || null
    case 'temps':
      return tempsText(effective?.nozzle_temp_min, effective?.nozzle_temp_max)
    case 'weight':
      return effective?.label_weight_g != null ? `${effective.label_weight_g} g` : null
    case 'note':
      return noteLine(spool.note)
    case 'added':
      return utcDay(spool.created_at)
    case 'qr':
      return null
  }
}

/**
 * #7216 — a spool label's lines: one for each chosen field with a value, in `ALL_FIELDS` order
 * (`label_pdf.py` `_spool_lines`). The QR code is no line. The number's `sizeMm` is its cap; the
 * label draws it smaller when it is too wide (`bigTextMm`), and the fit counts the cap.
 */
export function spoolLines(spool: SpoolDetail, fields: readonly LabelField[]): LabelLine[] {
  const lines: LabelLine[] = []
  for (const field of ALL_FIELDS) {
    if (!fields.includes(field)) continue
    const size = field === 'number' ? NUMBER_MM : FIELD_MM[field]
    if (size === undefined) continue
    const text = fieldText(spool, field)
    if (text) lines.push({ field, text, sizeMm: size, bold: field === 'number' || BOLD_FIELDS.has(field) })
  }
  return lines
}

/** Whether line `at` of `lines` has `GAP_MM` above it: the first line under the big number. */
export const gapAbove = (lines: readonly LabelLine[], at: number): boolean => at === 1 && lines[0]?.field === 'number'

/** The height a line takes in the fit: its gap and its line box (`label_pdf.py` `_Line.fit_height_mm`). */
const fitHeightMm = (lines: readonly LabelLine[], at: number): number => {
  const line = lines[at]!
  return (gapAbove(lines, at) ? GAP_MM : 0) + (line.field === 'number' ? line.sizeMm : line.sizeMm * LEADING)
}

/**
 * #7216, R-7216-10 — `lines` less the last ones while they are taller than a label of `heightMm`
 * less `PAD_MM` above and below (`label_pdf.py` `fit_lines`). The number always stays. Only the
 * line heights count, never a glyph's width, so the browser keeps the PDF's lines in another font.
 */
export function fitLines(lines: LabelLine[], heightMm: number): LabelLine[] {
  const available = heightMm - 2 * PAD_MM
  const kept = [...lines]
  const total = () => kept.reduce((sum, _, at) => sum + fitHeightMm(kept, at), 0)
  while (kept.length > 0 && kept.at(-1)!.field !== 'number' && total() > available + 1e-9) kept.pop()
  return kept
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
