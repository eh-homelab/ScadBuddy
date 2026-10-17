import { Loader2Icon } from 'lucide-react'

/**
 * The in-button busy icon (#3653: lucide's, as shadcn's buttons show one; a placeholder
 * for content still loading is a Skeleton). Decorative only: the busy state is
 * announced by the surrounding control's own text and `aria-busy`, so the spinner must
 * not join its accessible name.
 */
export function Spinner() {
  return <Loader2Icon aria-hidden="true" className="size-3.5 shrink-0 animate-spin opacity-70" />
}
