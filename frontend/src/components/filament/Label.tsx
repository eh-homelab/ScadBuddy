import type { SpoolDetail } from '../../api/filament'
import { LABEL_HEIGHT_MM, LABEL_WIDTH_MM, QR_MM } from './labelSheet'

/**
 * #6264 — one 50 x 25 mm spool label: the QR code (21 mm, drawn from `svg`), the spool
 * number large, the material and colour, the brand. It is print output, so it is black on
 * white in any theme; a long name is cut with an ellipsis and never leaves the 50 mm.
 */
export function Label({ spool, svg }: { spool: SpoolDetail; svg: string }) {
  const colour = spool.effective.colour_name ?? spool.colour_name
  const material = [spool.names?.material, spool.names?.subtype].filter(Boolean).join(' ')
  return (
    <div
      data-testid="label"
      className="box-border flex shrink-0 items-center gap-[1.5mm] overflow-hidden bg-white px-[2mm] text-black"
      style={{ width: `${LABEL_WIDTH_MM}mm`, height: `${LABEL_HEIGHT_MM}mm`, breakInside: 'avoid' }}
    >
      <div aria-hidden="true" className="shrink-0 [&>svg]:block [&>svg]:size-full" style={{ width: `${QR_MM}mm`, height: `${QR_MM}mm` }} dangerouslySetInnerHTML={{ __html: svg }} />
      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="text-[9mm] font-bold leading-none tabular-nums">#{spool.number}</span>
        <span className="mt-[1mm] truncate text-[3.2mm] font-semibold">{material}</span>
        <span className="truncate text-[3mm]">{colour}</span>
        <span className="truncate text-[2.6mm]">{spool.names?.brand}</span>
      </div>
    </div>
  )
}
