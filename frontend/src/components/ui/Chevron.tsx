import { ChevronRightIcon } from 'lucide-react'

/**
 * A fold's marker: pointing right when folded, down when open (#2172, #2173). An icon,
 * not a character, so the heading it sits in reads as its title alone. #3653 — lucide's
 * ChevronRightIcon, which a collapsible's trigger may use directly.
 */
export function Chevron({ open }: { open: boolean }) {
  return (
    <ChevronRightIcon
      aria-hidden="true"
      className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${open ? 'rotate-90' : ''}`}
    />
  )
}
