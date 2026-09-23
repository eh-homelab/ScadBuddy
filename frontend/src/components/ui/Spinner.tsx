/**
 * Decorative only: the busy state is announced by the surrounding control's own
 * text and `aria-busy`, so the spinner must not join its accessible name.
 */
export function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block size-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent opacity-70"
    />
  )
}
