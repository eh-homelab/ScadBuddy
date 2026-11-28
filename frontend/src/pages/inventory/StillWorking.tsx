import { Loader2Icon } from 'lucide-react'
import { Link } from 'react-router'

/**
 * #6949 K1 — a write the server accepted (its 202) and still runs. An inventory write passes
 * its operation id and `/activity?q=<id>`, where the Activities tab shows the operation; a
 * workflow passes no `href`. The page clears it when the write settles. `label` replaces the
 * first sentence (an order item's "Order fulfilled; stock still settling").
 */
export function StillWorking({ id, href, label = 'Still working.' }: { id: string; href?: string; label?: string }) {
  return (
    <p role="status" data-operation={id} className="flex w-full flex-wrap items-center gap-1.5 text-[13px] text-muted-foreground">
      <Loader2Icon aria-hidden className="size-3.5 shrink-0 motion-safe:animate-spin" />
      <span>{label} This can take a moment; you can leave this page.</span>
      {href && (
        <Link to={href} className="underline underline-offset-2 hover:text-foreground">
          See it in Activities
        </Link>
      )}
    </p>
  )
}
