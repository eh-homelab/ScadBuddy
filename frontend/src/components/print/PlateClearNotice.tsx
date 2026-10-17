import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { PrinterNow } from '../../api/types'
import { PlateWarning } from '../prints/PlateWarning'

/**
 * #3632 — the Prints page's "clear the plate" for the printer this dialog prints on,
 * where the next print is started. Read once per printer; a read that fails says
 * nothing here, since /prints is where the printers' state is followed.
 */
export function PlateClearNotice({ printerId }: { printerId: number }) {
  // Keyed by the printer it was read for, so a switch never shows the last one's plate.
  const [read, setRead] = useState<{ printerId: number; printer: PrinterNow | undefined } | null>(null)
  useEffect(() => {
    let live = true
    api
      .getPrintersNow()
      .then((now) => {
        if (live) setRead({ printerId, printer: now.printers?.find((entry) => entry.printer_id === printerId) })
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [printerId])
  const printer = read?.printerId === printerId ? read.printer : undefined
  if (!printer) return null
  return <PlateWarning printer={printer} className="" testId="print-plate-warning" />
}
