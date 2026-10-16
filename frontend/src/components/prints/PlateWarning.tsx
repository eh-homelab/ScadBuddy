import type { PrinterNow } from '../../api/types'

/**
 * #3632 — "clear the plate" as a warning, never a footnote: starting the next print on
 * a finished part can crash the nozzle into it. `null` is a plate ScadBuddy could not
 * read, which asks for a look rather than saying nothing.
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
    <p
      className={`flex items-start gap-1.5 rounded-[4px] border border-warn/40 bg-warn/10 px-2 py-1 text-[12.5px] font-medium text-warn ${className}`}
      data-testid={testId}
    >
      <svg aria-hidden="true" viewBox="0 0 16 16" className="mt-[2px] size-3.5 shrink-0" fill="currentColor">
        <path d="M8 1.5a1 1 0 0 1 .87.5l6.5 11.25A1 1 0 0 1 14.5 14.75h-13a1 1 0 0 1-.87-1.5L7.13 2A1 1 0 0 1 8 1.5Zm0 4a.75.75 0 0 0-.75.75v3.5a.75.75 0 0 0 1.5 0v-3.5A.75.75 0 0 0 8 5.5Zm0 6.25a.875.875 0 1 0 0 1.75.875.875 0 0 0 0-1.75Z" />
      </svg>
      <span>
        {flag
          ? `Clear the plate of ${printer.name} before the next print.`
          : `ScadBuddy could not tell whether the plate of ${printer.name} is clear. Check it before the next print.`}
      </span>
    </p>
  )
}
