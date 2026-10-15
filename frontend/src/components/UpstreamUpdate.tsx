import { useState } from 'react'
import { useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import type { MergePreview, UpstreamState } from '../api/types'
import { countConflicts, resolvePath } from '../lib/upstream'
import { useAsync } from '../lib/useAsync'
import { UnifiedDiff } from './UnifiedDiff'
import { Spinner } from './ui/Spinner'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'

/** The two states' badge colours: an update to take, an upstream that is gone. */
const UPDATE = 'shrink-0 bg-primary/15 text-primary'
const GONE = 'shrink-0'

/**
 * #160 — what a duplicate's card says about its upstream: an update to take, or an
 * upstream that no longer exists. Nothing when it is current or the update was dismissed.
 */
export function UpstreamBadge({ state }: { state: UpstreamState | null | undefined }) {
  if (state === 'update') {
    return (
      <Badge data-testid="update-badge" variant="secondary" className={UPDATE}>
        Update available
      </Badge>
    )
  }
  if (state === 'gone') {
    return (
      <Badge data-testid="upstream-gone" variant="destructive" className={GONE}>
        Upstream gone
      </Badge>
    )
  }
  return null
}

interface Props {
  slug: string
  state: UpstreamState | null | undefined
  /** After each action the record changed; only a merge changes the source too. */
  onChanged: (action: UpstreamAction) => void
}

type UpstreamAction = 'merge' | 'dismiss' | 'detach'

/**
 * #160 — the same badge in a duplicate's header, as a button: "Update available" opens
 * the update (upstream diff, merge result, Take update / Not now), "Upstream gone"
 * offers Detach. #235: a dismissed update is still mergeable, so it stays reachable
 * as a quieter "Update dismissed" link opening the same dialog.
 */
export function UpstreamUpdateButton({ slug, state, onChanged }: Props) {
  const [open, setOpen] = useState(false)
  if (state !== 'update' && state !== 'dismissed' && state !== 'gone') return null

  function done(action: UpstreamAction) {
    setOpen(false)
    onChanged(action)
  }

  if (state === 'dismissed') {
    return (
      <>
        <Badge asChild variant="ghost" className="shrink-0 cursor-pointer text-muted-foreground">
          <button type="button" data-testid="update-dismissed" onClick={() => setOpen(true)}>
            Update dismissed — review
          </button>
        </Badge>
        {open && (
          <UpdateDialog slug={slug} dismissed onClose={() => setOpen(false)} onDone={done} />
        )}
      </>
    )
  }

  return (
    <>
      <Badge
        asChild
        variant={state === 'update' ? 'secondary' : 'destructive'}
        className={`cursor-pointer ${state === 'update' ? `${UPDATE} hover:bg-primary/25` : `${GONE} hover:bg-destructive/20`}`}
      >
        <button type="button" data-testid={state === 'update' ? 'update-badge' : 'upstream-gone'} onClick={() => setOpen(true)}>
          {state === 'update' ? 'Update available' : 'Upstream gone'}
        </button>
      </Badge>
      {open && state === 'update' && (
        <UpdateDialog slug={slug} onClose={() => setOpen(false)} onDone={done} />
      )}
      {open && state === 'gone' && (
        <DetachDialog slug={slug} onClose={() => setOpen(false)} onDone={done} />
      )}
    </>
  )
}

interface DialogProps {
  slug: string
  onClose: () => void
  onDone: (action: UpstreamAction) => void
}

/**
 * `dismissed`: the update was set aside; it can still be taken, not dismissed again.
 * The prop only frames the dialog until the status arrives; from then on it offers
 * what the server reports now (#404), so a newer update is not missed.
 */
function UpdateDialog({
  slug,
  dismissed: openedDismissed = false,
  onClose,
  onDone,
}: DialogProps & { dismissed?: boolean }) {
  const navigate = useNavigate()
  const status = useAsync(() => api.getUpstream(slug), [slug])
  const [busy, setBusy] = useState<'merge' | 'dismiss' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const state = status.data?.state
  const dismissed = state ? state === 'dismissed' : openedDismissed
  const preview =
    state === 'update' || state === 'dismissed' ? (status.data?.preview ?? null) : null
  // Fetched, and nothing to offer: `current`, `gone`, or an update with no preview.
  const noUpdate = !!status.data && !preview

  function close() {
    if (!busy) onClose()
  }

  async function take() {
    setBusy('merge')
    setError(null)
    try {
      await api.mergeUpstream(slug)
      onDone('merge')
    } catch (caught) {
      // A conflicted merge writes nothing: its marked-up source is resolved in the editor.
      if (caught instanceof ApiError && caught.status === 409 && caught.problem['merge_base']) {
        await navigate(resolvePath(slug))
        return
      }
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      setBusy(null)
    }
  }

  async function dismiss() {
    setBusy('dismiss')
    setError(null)
    try {
      await api.dismissUpstream(slug)
      onDone('dismiss')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      setBusy(null)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] grid-rows-[auto_minmax(0,1fr)_auto] sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{noUpdate ? 'No update' : dismissed ? 'Dismissed update' : 'Update available'}</DialogTitle>
          {status.data && (
            <DialogDescription>
              {state === 'gone'
                ? `${status.data.upstream.id} no longer exists.`
                : noUpdate
                  ? `This copy is up to date with ${status.data.upstream.id}.`
                  : `${status.data.upstream.id} has changed since this copy was made or last updated.` +
                    (dismissed ? ' You dismissed this update; you can still take it.' : '')}
            </DialogDescription>
          )}
        </DialogHeader>
        <div className="-mx-4 min-h-0 overflow-y-auto px-4">
          {status.loading && (
            <div role="status" aria-label="Working out the merge" className="flex flex-col gap-2">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          )}
          {status.error && (
            <p role="alert" className="text-sm text-destructive">
              Could not load the update: {status.error.message}
            </p>
          )}
          {noUpdate && <p className="text-sm text-muted-foreground">There is no update to take any more.</p>}
          {status.data && preview && <UpdatePreview preview={preview} />}
          {error && (
            <p role="alert" className="mt-3 text-sm text-destructive">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          {dismissed || noUpdate ? (
            <Button type="button" variant="outline" onClick={close} disabled={!!busy}>
              Close
            </Button>
          ) : (
            <Button type="button" variant="outline" onClick={() => void dismiss()} disabled={!preview || !!busy}>
              {busy === 'dismiss' && <Spinner />}
              Not now
            </Button>
          )}
          <Button type="button" onClick={() => void take()} disabled={!preview || !!busy}>
            {busy === 'merge' && <Spinner />}
            Take update
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function UpdatePreview({ preview }: { preview: MergePreview }) {
  const conflicts = countConflicts(preview.merged)

  return (
    <div className="flex flex-col gap-4">
      <section>
        <h3 className="mb-1.5 text-sm font-medium">Upstream changes</h3>
        <div className="max-h-56 overflow-y-auto rounded-lg border bg-ui-muted/40">
          {/* From the upstream at `base`, where it lived then, to now (#236). */}
          <UnifiedDiff patch={preview.patch} />
        </div>
      </section>

      <section>
        <h3 className="mb-1.5 text-sm font-medium">Merge result</h3>
        {preview.clean ? (
          <p data-testid="merge-verdict" className="mb-1.5 text-xs text-ok">
            Merges cleanly with your edits.
          </p>
        ) : (
          <p data-testid="merge-verdict" className="mb-1.5 text-xs text-destructive">
            {conflicts === 1 ? '1 conflict' : `${conflicts} conflicts`} with your edits. Taking
            the update opens the marked-up source in the editor to resolve.
          </p>
        )}
        <pre
          data-testid="merge-result"
          className="sb-num max-h-56 overflow-auto rounded-lg border bg-ui-muted/40 p-3 text-xs leading-[1.5] whitespace-pre"
        >
          {preview.merged}
        </pre>
        {(preview.taken ?? []).length > 0 && (
          <p className="mt-1.5 text-xs text-muted-foreground">
            Also takes: <span className="sb-num">{(preview.taken ?? []).join(', ')}</span>
          </p>
        )}
        {(preview.kept ?? []).length > 0 && (
          <p className="mt-1.5 text-xs text-muted-foreground">
            Keeps yours, changed on both sides:{' '}
            <span className="sb-num">{(preview.kept ?? []).join(', ')}</span>
          </p>
        )}
      </section>
    </div>
  )
}

function DetachDialog({ slug, onClose, onDone }: DialogProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function close() {
    if (!busy) onClose()
  }

  async function detach() {
    setBusy(true)
    setError(null)
    try {
      await api.detachUpstream(slug)
      onDone('detach')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
      setBusy(false)
    }
  }

  return (
    <AlertDialog open onOpenChange={(open) => !open && close()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Upstream gone</AlertDialogTitle>
          <AlertDialogDescription>
            The template this one was duplicated from no longer exists, so there are no more updates to take. Detach
            it to keep this as an ordinary template of yours.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <Button type="button" onClick={() => void detach()} disabled={busy}>
            {busy && <Spinner />}
            Detach
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
