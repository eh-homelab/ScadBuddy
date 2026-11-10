import { useEffect, useState } from 'react'
import { BoxIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

/** #5863 — how long a failed picture waits before it is tried again; each later try of
 * the same picture waits twice as long. */
export const PICTURE_RETRY_MS = 15_000
/** #5877 — how often a picture is loaded before it is given up: the first load and
 * three more, after 15 s, 30 s and 60 s. A picture that is never there (a deleted
 * output's, a printer's job with no cover) costs four requests, not one every few
 * minutes for as long as the card is on the page. */
export const PICTURE_MAX_TRIES = 4

type Props = { src: string | null | undefined; className?: string }

/**
 * #3742 — the picture of a queue item or a printer's job: the URL the server gives
 * (`picture`: ScadBuddy's own image of its job, else Bambuddy's through ScadBuddy's farm
 * proxy). With none, or one that fails to load, a neutral placeholder, never a broken
 * image. A failed picture is tried again later (#5863): Bambuddy may still be taking a
 * cover out of the job's file when the card first asks for it. After
 * {@link PICTURE_MAX_TRIES} failed loads it is given up (#5877). Any change of URL starts
 * over, a URL that comes back after another included (#5877): the count is the URL's
 * while it is shown, never kept. The job's name is always beside it, so the picture is
 * decorative (empty alt).
 */
export function JobPicture({ src, className }: Props) {
  // Keyed by the URL, so a new one, or one that comes back, starts with no failures.
  return <Picture key={src ?? ''} src={src} className={className} />
}

function Picture({ src, className }: Props) {
  /** How often this picture failed to load, and whether it now waits for a next try. */
  const [failures, setFailures] = useState(0)
  const [waiting, setWaiting] = useState(false)

  useEffect(() => {
    if (!waiting) return
    const timer = setTimeout(() => setWaiting(false), PICTURE_RETRY_MS * 2 ** (failures - 1))
    return () => clearTimeout(timer)
  }, [waiting, failures])

  const shown = src && !waiting && failures < PICTURE_MAX_TRIES ? src : null
  return (
    <span
      className={cn('flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-md border bg-muted', className)}
      data-testid="job-picture"
    >
      {shown ? (
        <img
          // A new element per try, so the browser asks again.
          key={failures}
          src={shown}
          alt=""
          loading="lazy"
          className="size-full object-contain"
          onError={() => {
            const failed = failures + 1
            setFailures(failed)
            // The last try's failure is final: no timer is left behind.
            setWaiting(failed < PICTURE_MAX_TRIES)
          }}
        />
      ) : (
        <BoxIcon aria-hidden="true" className="size-5 text-muted-foreground" data-testid="job-picture-placeholder" />
      )}
    </span>
  )
}
