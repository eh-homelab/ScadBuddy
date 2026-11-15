import { useEffect, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { ListingPrepareProgress } from '../../api/types'
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'

/** How often the progress is read again while renders are in the queue. */
export const PREPARE_POLL_MS = 3000

function combinations(count: number): string {
  return count === 1 ? '1 colour combination' : `${count} colour combinations`
}

/**
 * #6237 — "Prepare every colour combination" for a published listing: renders each
 * combination of its colour choices before a customer asks, so a colour change shows at
 * once. It reads the count before it starts (`GET …/prepare`), asks to confirm, then shows
 * "n of N ready", read again every `PREPARE_POLL_MS` while the run is open or a render is
 * in the queue. The run renders a few at a time, at the normal priority (#6299 review). It
 * shows nothing for a listing with no colour to choose. Over the cap it says the count and
 * the cap, and offers no start. Combinations whose render could not be started and is not
 * ready since (`failed_to_start`) are named, with a way to prepare again; "Prepare again"
 * also renders a failed combination again (#6299 review). A failed read does not stop the
 * reads, and the next read that succeeds clears its error. `version` is the listing's
 * `updated_at`: a new publish reads it again.
 */
export function PrepareColours({ listingId, version, pollMs = PREPARE_POLL_MS }: { listingId: string; version: string; pollMs?: number }) {
  const [progress, setProgress] = useState<ListingPrepareProgress | null>(null)
  // A failed read and a failed start are kept apart: a later read that succeeds clears
  // only the read's error (#6299 review).
  const [readError, setReadError] = useState<string | null>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reads, setReads] = useState(0)
  // Counts the reads that finished, with an answer or a failure: each one schedules the next.
  const [settled, setSettled] = useState(0)

  useEffect(() => {
    let live = true
    api.storefrontListingPrepare(listingId).then(
      (read) => {
        if (!live) return
        setProgress(read)
        setReadError(null)
        setSettled((n) => n + 1)
      },
      (failure: unknown) => {
        if (!live) return
        setReadError(failure instanceof ApiError ? failure.detail : 'The colour combinations could not be read.')
        setSettled((n) => n + 1)
      },
    )
    return () => {
      live = false
    }
  }, [listingId, version, reads])

  const running = progress !== null && (progress.running || progress.rendering > 0)
  // #6299 review: one failed read does not stop the polling; the next read is set after
  // each read that finished.
  useEffect(() => {
    if (!running) return
    const timer = setTimeout(() => setReads((n) => n + 1), pollMs)
    return () => clearTimeout(timer)
  }, [running, settled, pollMs])

  const error = startError ?? readError

  async function start() {
    setConfirming(false)
    setBusy(true)
    setStartError(null)
    try {
      await api.prepareStorefrontListing(listingId)
    } catch (failure) {
      setStartError(failure instanceof ApiError ? failure.detail : 'The renders could not be started.')
    } finally {
      setBusy(false)
      setReads((n) => n + 1)
    }
  }

  if (progress === null) {
    return error ? (
      <p role="alert" className="text-[13px] text-warn">
        {error}
      </p>
    ) : null
  }
  const { count, cap, ready, failed, failed_to_start: notStarted } = progress
  if (count === 0) return null
  const over = count > cap
  const started = progress.running || ready + failed + progress.rendering + notStarted > 0
  const done = ready === count

  return (
    <section aria-label="Colour combinations" className="flex flex-col gap-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <p className="mr-auto text-sm">
          <span className="font-medium">{combinations(count)}</span>
          {started && !over && (
            <span className="text-muted-foreground">
              {' '}
              ({ready} of {count} ready{failed > 0 ? `, ${failed} failed` : ''})
            </span>
          )}
        </p>
        {!over && !done && !running && (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => setConfirming(true)}>
            {started ? 'Prepare again' : 'Prepare every colour combination'}
          </Button>
        )}
      </div>
      {started && !over && (
        <div
          role="progressbar"
          aria-label="Colour combinations ready"
          aria-valuemin={0}
          aria-valuemax={count}
          aria-valuenow={ready}
          className="h-1 overflow-hidden rounded-full bg-muted"
        >
          <div className="h-full bg-primary transition-[width] motion-reduce:transition-none" style={{ width: `${(ready / count) * 100}%` }} />
        </div>
      )}
      {over && (
        <p className="text-[13px] text-muted-foreground">
          This listing has {count} colour combinations. The shop prepares at most {cap} at a time. Offer fewer colours to prepare them.
        </p>
      )}
      {!over && !started && <p className="text-[13px] text-muted-foreground">Render each one now, so a customer who changes a colour sees it at once.</p>}
      {notStarted > 0 && !running && !done && (
        <p className="text-[13px] text-muted-foreground">
          {notStarted === 1 ? 'The render of 1 combination' : `The renders of ${notStarted} combinations`} could not be started. Prepare again to try them again.
        </p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Prepare {combinations(count)}?</AlertDialogTitle>
            <AlertDialogDescription>
              ScadBuddy renders a few at a time, so your own renders and your customers' are not held up. The other choices stay at their defaults.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button type="button" onClick={() => void start()}>
              Prepare
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}
