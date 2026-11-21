import type { ReactNode } from 'react'
import { ChevronDownIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

export type SummaryId = 'printer' | 'nozzle' | 'plate' | 'quality'

export interface SummaryItem {
  id: SummaryId
  label: string
  /** What prints: the printer's name, the nozzle, the bed plate or the process. */
  value: ReactNode
  /**
   * A control that takes the cell's place: the printer's select opens in place, as its
   * own list. Its trigger takes `summaryCell` for its class.
   */
  control?: ReactNode
  /** Simple: the step that opens under the row. Advanced: none, `onSelect` goes to its section. */
  panel?: ReactNode
  onSelect?: () => void
  /** A note on the value, such as a bed plate to swap before the print starts. */
  note?: ReactNode
}

/** The look of one cell, for a control that takes a cell's place. */
export const summaryCell =
  'relative flex h-auto data-[size=default]:h-auto min-h-14 w-full min-w-0 flex-col items-start justify-center gap-0.5 rounded-none border-0 bg-transparent px-3 py-2 text-left shadow-none outline-none transition-colors hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-transparent dark:hover:bg-muted/60'

/**
 * #6885 — one row that says what will print: the printer, the nozzle, the bed plate and
 * the quality, in both modes. In Simple, a cell opens its step under the row, so a
 * choice is changed where it is read. In Advanced, a cell opens the section that holds
 * its step. A cell with no step and no section only shows its value.
 */
export function PrintSummary({
  items,
  expanded,
  onExpand,
}: {
  items: SummaryItem[]
  expanded: SummaryId | null
  onExpand: (id: SummaryId | null) => void
}) {
  const open = items.find((item) => item.id === expanded && item.panel)
  const notes = items.filter((item) => item.note && item.id !== open?.id)
  return (
    <div data-testid="print-summary">
      {/* Each cell draws its right and bottom edges; the negative margin hides the outer
          ones, so the lines stay right however the cells wrap. */}
      <div className="overflow-hidden rounded-lg border bg-card">
        <div className="-mr-px -mb-px grid grid-cols-[repeat(auto-fit,minmax(9rem,1fr))] [&>*]:border-r [&>*]:border-b">
        {items.map((item) => {
          if (item.control) return <div key={item.id} className="min-w-0">{item.control}</div>
          const value = (
            <>
              <span className="text-xs text-muted-foreground" id={`print-summary-${item.id}-label`}>
                {item.label}
              </span>
              <span className="flex w-full min-w-0 items-center gap-1 text-sm font-medium">
                <span className="min-w-0 truncate" data-testid={`print-summary-${item.id}`}>
                  {item.value}
                </span>
                {item.note && <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-primary" />}
              </span>
            </>
          )
          if (!item.panel && !item.onSelect) {
            return (
              <div key={item.id} className={cn(summaryCell, 'hover:bg-transparent')}>
                {value}
              </div>
            )
          }
          const isOpen = open?.id === item.id
          return (
            <button
              key={item.id}
              type="button"
              className={cn(summaryCell, 'relative cursor-pointer pr-7', isOpen && 'bg-muted')}
              aria-expanded={item.panel ? isOpen : undefined}
              aria-controls={item.panel && isOpen ? 'print-summary-panel' : undefined}
              onClick={() => (item.panel ? onExpand(isOpen ? null : item.id) : item.onSelect?.())}
              data-testid={`print-summary-${item.id}-open`}
            >
              {value}
              <ChevronDownIcon
                aria-hidden="true"
                className={cn(
                  'absolute top-1/2 right-2 size-4 -translate-y-1/2 text-muted-foreground transition-transform motion-reduce:transition-none',
                  item.panel ? isOpen && 'rotate-180' : '-rotate-90',
                )}
              />
            </button>
          )
        })}
        </div>
      </div>
      {open && (
        <div id="print-summary-panel" className="mt-2" data-testid={`print-summary-panel-${open.id}`}>
          {open.panel}
        </div>
      )}
      {notes.map((item) => (
        <p key={item.id} role="status" className="mt-1.5 text-xs text-muted-foreground" data-testid={`print-summary-${item.id}-note`}>
          {item.note}
        </p>
      ))}
    </div>
  )
}
