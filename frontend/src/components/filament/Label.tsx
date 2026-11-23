import type { SpoolDetail } from '../../api/filament'
import { bigTextMm, DEFAULT_SIZE, type LabelSize, QR_MM, QUIET_MM } from './labelSheet'

/** The box both labels share: black on white in any theme, the QR box and the text column centred top to bottom. */
const box = (size: LabelSize) => ({
  width: `${size.widthMm}mm`,
  height: `${size.heightMm}mm`,
  breakInside: 'avoid' as const,
  paddingLeft: `${QUIET_MM}mm`,
  paddingRight: '1.5mm',
  gap: `${QUIET_MM}mm`,
})

/**
 * #6264 — one spool label, 50 x 25 mm unless `size` says otherwise (#7157): the QR code (19.8 mm with a 2.6 mm
 * quiet zone, drawn from `svg`), the spool number large, the material and colour, the brand. It is print output,
 * so it is black on white in any theme; a long name is cut with an ellipsis and never leaves the label. The PDF
 * (`backend/scadbuddy/filament/label_pdf.py`) draws the same lines.
 */
export function Label({ spool, svg, size = DEFAULT_SIZE, testId = 'label' }: { spool: SpoolDetail; svg: string; size?: LabelSize; testId?: string }) {
  const colour = spool.effective.colour_name ?? spool.colour_name
  const number = `#${spool.number}`
  // 9 mm for up to #999 on a 50 mm label; a longer number, or a narrower label, gets smaller so it fits the text column.
  const numberMm = bigTextMm(number, 9, size.widthMm)
  const material = [spool.names?.material, spool.names?.subtype].filter(Boolean).join(' ')
  return (
    <div data-testid={testId} className="box-border flex shrink-0 items-center overflow-hidden bg-white text-black" style={box(size)}>
      <div aria-hidden="true" className="shrink-0 [&>svg]:block [&>svg]:size-full" style={{ width: `${QR_MM}mm`, height: `${QR_MM}mm` }} dangerouslySetInnerHTML={{ __html: svg }} />
      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="whitespace-nowrap font-bold leading-none tabular-nums" style={{ fontSize: `${numberMm}mm` }}>
          {number}
        </span>
        <span className="mt-[1mm] truncate text-[3.2mm] font-semibold">{material}</span>
        <span className="truncate text-[3mm]">{colour}</span>
        <span className="truncate text-[2.6mm]">{spool.names?.brand}</span>
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
        <span className="whitespace-nowrap font-bold leading-none tabular-nums" style={{ fontSize: `${codeMm}mm` }}>
          {code}
        </span>
        <span className="mt-[1mm] line-clamp-2 break-words text-[3.2mm] font-semibold" title={name}>
          {name}
        </span>
      </div>
    </div>
  )
}
