import type { ReactNode } from 'react'
import { ChevronDownIcon } from 'lucide-react'
import { cn } from '@/lib/utils'
import { SECTION_TITLES, type SectionId } from './printSections'

/**
 * #6885 — one of Advanced's collapsible sections. A closed section keeps its steps
 * mounted but hidden, so a step's own checks (a process the size no longer offers) still
 * run, and nothing it holds is lost.
 */
export function PrintSection({
  id,
  open,
  onToggle,
  summary,
  children,
}: {
  id: SectionId
  open: boolean
  onToggle: (open: boolean) => void
  /** What the section holds now, said beside its title while it is closed. */
  summary?: ReactNode
  children: ReactNode
}) {
  const title = SECTION_TITLES[id]
  return (
    <section
      id={`print-section-${id}`}
      aria-labelledby={`print-section-${id}-title`}
      className="scroll-mt-2"
      data-testid={`print-section-${id}`}
    >
      <h3 className="m-0">
        {/* Named by its title alone; the summary beside it is read after. */}
        <button
          type="button"
          aria-labelledby={`print-section-${id}-title`}
          aria-expanded={open}
          aria-controls={`print-section-${id}-body`}
          onClick={() => onToggle(!open)}
          className="flex w-full cursor-pointer items-center gap-2 rounded-md border-b py-2 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <ChevronDownIcon
            aria-hidden="true"
            className={cn(
              'size-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none',
              !open && '-rotate-90',
            )}
          />
          <span id={`print-section-${id}-title`} className="text-sm font-semibold">
            {title}
          </span>
          {!open && summary && (
            <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{summary}</span>
          )}
        </button>
      </h3>
      <div id={`print-section-${id}-body`} hidden={!open} className="space-y-3 pt-3">
        {children}
      </div>
    </section>
  )
}
