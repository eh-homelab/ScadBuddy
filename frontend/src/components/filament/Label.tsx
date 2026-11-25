import type { SpoolDetail } from '../../api/filament'
import { cn } from '@/lib/utils'
import { BIG_FONT_FAMILY, bigTextMm, DEFAULT_FIELDS, DEFAULT_SIZE, fitLines, gapAbove, type LabelField, type LabelSize, PAD_MM, QR_MM, QUIET_MM, spoolLines } from './labelSheet'

/** The box both labels share: black on white in any theme, the QR box and the text column centred top to bottom. */
const box = (size: LabelSize) => ({
  width: `${size.widthMm}mm`,
  height: `${size.heightMm}mm`,
  breakInside: 'avoid' as const,
  paddingLeft: `${QUIET_MM}mm`,
  paddingRight: `${PAD_MM}mm`,
  gap: `${QUIET_MM}mm`,
})

/**
 * #6264 — one spool label, 50 x 25 mm unless `size` says otherwise (#7157): the QR code (19.8 mm with a 2.6 mm
 * quiet zone, drawn from `svg`) and a line for each chosen field (#7216, `fields`; by default the number large,
 * the material and colour, the brand). Lines that do not fit the label's height are left off from the bottom
 * (`fitLines`), and with no `qr` field the text column takes the QR box's width too. It is print output, so it
 * is black on white in any theme; a long name is cut with an ellipsis and never leaves the label. The PDF
 * (`backend/scadbuddy/filament/label_pdf.py`) draws the same lines.
 */
export function Label({
  spool,
  svg,
  size = DEFAULT_SIZE,
  fields = DEFAULT_FIELDS,
  testId = 'label',
}: {
  spool: SpoolDetail
  svg: string
  size?: LabelSize
  fields?: readonly LabelField[]
  testId?: string
}) {
  const qr = fields.includes('qr')
  const lines = fitLines(spoolLines(spool, fields), size.heightMm)
  return (
    <div data-testid={testId} className="box-border flex shrink-0 items-center overflow-hidden bg-white text-black" style={box(size)}>
      {qr && <div aria-hidden="true" className="shrink-0 [&>svg]:block [&>svg]:size-full" style={{ width: `${QR_MM}mm`, height: `${QR_MM}mm` }} dangerouslySetInnerHTML={{ __html: svg }} />}
      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        {lines.map((line, at) =>
          line.field === 'number' ? (
            // 9 mm for up to #999 on a 50 mm label; a longer number, or a narrower column, gets smaller so it fits.
            <span key={line.field} className="whitespace-nowrap font-bold leading-none tabular-nums" style={{ fontSize: `${bigTextMm(line.text, line.sizeMm, size.widthMm, qr)}mm`, fontFamily: BIG_FONT_FAMILY }}>
              {line.text}
            </span>
          ) : (
            <span key={line.field} className={cn('truncate', line.bold && 'font-semibold', gapAbove(lines, at) && 'mt-[1mm]')} style={{ fontSize: `${line.sizeMm}mm` }}>
              {line.text}
            </span>
          ),
        )}
      </div>
    </div>
  )
}

/**
 * #6955 — one place label (a printer, an AMS unit or slot, the external holder), 50 x 25 mm unless `size` says
 * otherwise (#7157): the same QR box as the spool label, the code large, the place's name below it in at most two lines.
 */
export function LocationLabel({ code, name, svg, size = DEFAULT_SIZE, testId = 'label' }: { code: string; name: string; svg: string; size?: LabelSize; testId?: string }) {
  // 8 mm up to "L-999" on a 50 mm label; a longer code, or a narrower label, gets smaller so it fits the text column.
  const codeMm = bigTextMm(code, 8, size.widthMm)
  return (
    <div data-testid={testId} className="box-border flex shrink-0 items-center overflow-hidden bg-white text-black" style={box(size)}>
      <div aria-hidden="true" className="shrink-0 [&>svg]:block [&>svg]:size-full" style={{ width: `${QR_MM}mm`, height: `${QR_MM}mm` }} dangerouslySetInnerHTML={{ __html: svg }} />
      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="whitespace-nowrap font-bold leading-none tabular-nums" style={{ fontSize: `${codeMm}mm`, fontFamily: BIG_FONT_FAMILY }}>
          {code}
        </span>
        <span className="mt-[1mm] line-clamp-2 break-words text-[3.2mm] font-semibold" title={name}>
          {name}
        </span>
      </div>
    </div>
  )
}
