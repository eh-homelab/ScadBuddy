import { useEffect, useState } from 'react'
import { BoxIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

/** #5863 — how long a failed picture waits before it is tried again; each later try of
 * the same picture waits twice as long, up to {@link PICTURE_RETRY_MAX_MS}. */
export const PICTURE_RETRY_MS = 15_000
export const PICTURE_RETRY_MAX_MS = 300_000

/** A picture that failed: how often, and whether it now waits for its next try. */
interface Failure {
  src: string
  tries: number
  waiting: boolean
}

/**
 * #3742 — the picture of a queue item or a printer's job: the URL the server gives
 * (`picture`: ScadBuddy's own image of its job, else Bambuddy's through ScadBuddy's farm
 * proxy). With none, or one that fails to load, a neutral placeholder, never a broken
 * image. A failed picture is tried again later (#5863): Bambuddy may still be taking a
 * cover out of the job's file when the card first asks for it. The job's name is always
 * beside it, so the picture is decorative (empty alt).
 */
export function JobPicture({ src, className }: { src: string | null | undefined; className?: string }) {
  const [failure, setFailure] = useState<Failure | null>(null)
  // Only a failure of this picture counts: a new picture is shown at once.
  const mine = failure !== null && failure.src === src ? failure : null
  const waiting = mine?.waiting ?? false
  const tries = mine?.tries ?? 0

  useEffect(() => {
    if (!waiting) return
    const delay = Math.min(PICTURE_RETRY_MS * 2 ** (tries - 1), PICTURE_RETRY_MAX_MS)
    const timer = setTimeout(() => setFailure((last) => last && { ...last, waiting: false }), delay)
    return () => clearTimeout(timer)
  }, [waiting, tries])

  const shown = src && !waiting ? src : null
  return (
    <span
      className={cn('flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-md border bg-muted', className)}
      data-testid="job-picture"
    >
      {shown ? (
        <img
          // A new element per try, so the browser asks again.
          key={`${shown}#${tries}`}
          src={shown}
          alt=""
          loading="lazy"
          className="size-full object-contain"
          onError={() => setFailure({ src: shown, tries: tries + 1, waiting: true })}
        />
      ) : (
        <BoxIcon aria-hidden="true" className="size-5 text-muted-foreground" data-testid="job-picture-placeholder" />
      )}
    </span>
  )
}
