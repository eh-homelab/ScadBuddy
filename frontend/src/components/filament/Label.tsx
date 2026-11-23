import type { SpoolDetail } from '../../api/filament'
import { LABEL_HEIGHT_MM, LABEL_WIDTH_MM, QR_MM, QUIET_MM } from './labelSheet'

/**
 * #6264 — one 50 x 25 mm spool label: the QR code (19.8 mm with a 2.6 mm quiet zone, drawn from `svg`), the spool
 * number large, the material and colour, the brand. It is print output, so it is black on
 * white in any theme; a long name is cut with an ellipsis and never leaves the 50 mm.
 */
export function Label({ spool, svg, testId = 'label' }: { spool: SpoolDetail; svg: string; testId?: string }) {
  const colour = spool.effective.colour_name ?? spool.colour_name
  const number = `#${spool.number}`
  // 9 mm for up to #999; a longer number gets smaller so it fits the 23.5 mm text column.
  const numberMm = Math.min(9, 22 / (0.62 * number.length))
  const material = [spool.names?.material, spool.names?.subtype].filter(Boolean).join(' ')
  return (
    <div
      data-testid={testId}
      className="box-border flex shrink-0 items-center overflow-hidden bg-white text-black"
      style={{ width: `${LABEL_WIDTH_MM}mm`, height: `${LABEL_HEIGHT_MM}mm`, breakInside: 'avoid', paddingLeft: `${QUIET_MM}mm`, paddingRight: '1.5mm', gap: `${QUIET_MM}mm` }}
    >
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
 * #6955 — one 50 x 25 mm place label (a printer, an AMS unit or slot, the external holder): the same QR
 * box as the spool label, the code large, the place's name below it in at most two lines.
 */
export function LocationLabel({ code, name, svg, testId = 'label' }: { code: string; name: string; svg: string; testId?: string }) {
  // 8 mm up to "L-999" (5 characters); a longer code gets smaller so it fits the 23.5 mm text column.
  const codeMm = Math.min(8, 22 / (0.62 * code.length))
  return (
    <div
      data-testid={testId}
      className="box-border flex shrink-0 items-center overflow-hidden bg-white text-black"
      style={{ width: `${LABEL_WIDTH_MM}mm`, height: `${LABEL_HEIGHT_MM}mm`, breakInside: 'avoid', paddingLeft: `${QUIET_MM}mm`, paddingRight: '1.5mm', gap: `${QUIET_MM}mm` }}
    >
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
