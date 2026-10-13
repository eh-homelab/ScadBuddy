/**
 * A fold's marker: pointing right when folded, down when open (#2172, #2173). Drawn, not
 * a character, so the heading it sits in reads as its title alone.
 */
export function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      className={`size-3 shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
