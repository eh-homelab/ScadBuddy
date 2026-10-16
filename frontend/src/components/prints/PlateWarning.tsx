import { TriangleAlertIcon } from 'lucide-react'
import type { PrinterNow } from '../../api/types'
import { Alert, AlertTitle } from '@/components/ui/alert'
import { cn } from '@/lib/utils'

/**
 * #3632 — "clear the plate" as a warning, never a footnote: starting the next print on
 * a finished part can crash the nozzle into it. `null` is a plate ScadBuddy could not
 * read, which asks for a look rather than saying nothing. No live role: it is read on
 * every poll, and stays while the plate does.
 */
export function PlateWarning({
  printer,
  className = 'mt-1.5',
  testId,
}: {
  printer: Pick<PrinterNow, 'name' | 'awaiting_plate_clear'>
  className?: string
  testId?: string
}) {
  const flag = printer.awaiting_plate_clear
  if (flag === false || flag === undefined) return null
  return (
    <Alert role={undefined} className={cn('border-warn/40 bg-warn/10 text-warn', className)} data-testid={testId}>
      <TriangleAlertIcon aria-hidden="true" />
      <AlertTitle className="text-[12.5px] leading-snug">
        {flag
          ? `Clear the plate of ${printer.name} before the next print.`
          : `ScadBuddy could not tell whether the plate of ${printer.name} is clear. Check it before the next print.`}
      </AlertTitle>
    </Alert>
  )
}
