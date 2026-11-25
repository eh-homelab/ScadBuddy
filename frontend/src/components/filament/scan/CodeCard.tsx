import { ExternalLinkIcon } from 'lucide-react'
import { Link } from 'react-router'
import type { Resolved } from '../../../api/filament'
import { codeLabel, codeTarget } from '../../../lib/resolvedCode'
import { Button } from '@/components/ui/button'

/**
 * #7273 — a tag or label whose records name a thing by a typed code (#6955) or a
 * `scadbuddy:spool:<id>` text (`next` of `open_code`): its kind and code, its name when it has
 * one, whether it is archived, and "Open". The tag is not offered for binding here: a tag that
 * names a place is a place's tag; a spool's own page can still bind it.
 */
export function CodeCard({ resolved }: { resolved: Resolved }) {
  const name = codeLabel(resolved)
  return (
    <section aria-label={name} className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <div className="flex min-w-0 flex-col gap-0.5">
        <h2 className="text-lg font-semibold tracking-tight tabular-nums">{name}</h2>
        {resolved.name && <p className="break-words">{resolved.name}</p>}
        {resolved.archived && <p className="text-muted-foreground">Archived</p>}
      </div>
      <Button asChild variant="outline" className="h-11">
        <Link to={codeTarget(resolved)}>
          <ExternalLinkIcon /> Open
        </Link>
      </Button>
    </section>
  )
}
