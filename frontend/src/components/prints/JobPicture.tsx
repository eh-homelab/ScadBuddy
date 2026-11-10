import { useState } from 'react'
import { BoxIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * #3742 — the picture of a queue item or a printer's job: the URL the server gives
 * (`picture`: ScadBuddy's own image of its job, else Bambuddy's through ScadBuddy's farm
 * proxy). With none, or one that fails to load, a neutral placeholder, never a broken
 * image. The job's name is always beside it, so the picture is decorative (empty alt).
 */
export function JobPicture({ src, className }: { src: string | null | undefined; className?: string }) {
  // The URL that failed, so a new picture (the next job) is tried again.
  const [failed, setFailed] = useState<string | null>(null)
  const shown = src && src !== failed ? src : null
  return (
    <span
      className={cn('flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-md border bg-muted', className)}
      data-testid="job-picture"
    >
      {shown ? (
        <img
          key={shown}
          src={shown}
          alt=""
          loading="lazy"
          className="size-full object-contain"
          onError={() => setFailed(shown)}
        />
      ) : (
        <BoxIcon aria-hidden="true" className="size-5 text-muted-foreground" data-testid="job-picture-placeholder" />
      )}
    </span>
  )
}
