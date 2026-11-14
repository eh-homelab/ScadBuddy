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
 * "n of N ready", read again every `PREPARE_POLL_MS` while a render is in the queue. It
 * shows nothing for a listing with no colour to choose. Over the cap it says the count and
 * the cap, and offers no start. `version` is the listing's `updated_at`: a new publish
 * reads it again.
 */
export function PrepareColours({ listingId, version, pollMs = PREPARE_POLL_MS }: { listingId: string; version: string; pollMs?: number }) {
  const [progress, setProgress] = useState<ListingPrepareProgress | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  /** Combinations the last start left out (a full queue, or the request's time ran out). */
  const [leftOut, setLeftOut] = useState(0)
  const [reads, setReads] = useState(0)

  useEffect(() => {
    let live = true
    api.storefrontListingPrepare(listingId).then(
      (read) => {
        if (live) setProgress(read)
      },
      (failure: unknown) => {
        if (live) setError(failure instanceof ApiError ? failure.detail : 'The colour combinations could not be read.')
      },
    )
    return () => {
      live = false
    }
  }, [listingId, version, reads])

  const running = progress !== null && progress.running > 0
  useEffect(() => {
    if (!running) return
    const timer = setTimeout(() => setReads((n) => n + 1), pollMs)
    return () => clearTimeout(timer)
  }, [running, progress, pollMs])

  async function start() {
    setConfirming(false)
    setBusy(true)
    setError(null)
    try {
      const started = await api.prepareStorefrontListing(listingId)
      setLeftOut(started.count - started.queued)
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : 'The renders could not be started.')
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
  const { count, cap, ready, failed } = progress
  if (count === 0) return null
  const over = count > cap
  const started = ready + failed + progress.running > 0
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
            {started ? 'Prepare the rest' : 'Prepare every colour combination'}
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
      {leftOut > 0 && !running && !done && (
        <p className="text-[13px] text-muted-foreground">{leftOut === 1 ? '1 combination was' : `${leftOut} combinations were`} not queued, because the queue was busy. Prepare the rest to queue them.</p>
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
              ScadBuddy renders each combination after every other render, so customers and your own renders go first. The other choices stay at their defaults.
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
