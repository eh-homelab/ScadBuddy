import { useState } from 'react'
import { useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import type { MergePreview, UpstreamState, UpstreamStatus } from '../api/types'
import { countConflicts, resolvePath } from '../lib/upstream'
import { useAsync } from '../lib/useAsync'
import { UnifiedDiff } from './UnifiedDiff'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

const BADGE = 'shrink-0 rounded-[6px] px-1.5 py-0.5 text-[11px]'

/**
 * #160 — what a duplicate's card says about its upstream: an update to take, or an
 * upstream that no longer exists. Nothing when it is current or the update was dismissed.
 */
export function UpstreamBadge({ state }: { state: UpstreamState | null | undefined }) {
  if (state === 'update') {
    return (
      <span data-testid="update-badge" className={`${BADGE} bg-accent/12 text-accent`}>
        Update available
      </span>
    )
  }
  if (state === 'gone') {
    return (
      <span data-testid="upstream-gone" className={`${BADGE} bg-warn/10 text-warn`}>
        Upstream gone
      </span>
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
 * offers Detach.
 */
export function UpstreamUpdateButton({ slug, state, onChanged }: Props) {
  const [open, setOpen] = useState(false)
  if (state !== 'update' && state !== 'gone') return null

  function done(action: UpstreamAction) {
    setOpen(false)
    onChanged(action)
  }

  return (
    <>
      <button
        type="button"
        data-testid={state === 'update' ? 'update-badge' : 'upstream-gone'}
        onClick={() => setOpen(true)}
        className={
          state === 'update'
            ? `${BADGE} bg-accent/12 text-accent hover:bg-accent/20`
            : `${BADGE} bg-warn/10 text-warn hover:bg-warn/15`
        }
      >
        {state === 'update' ? 'Update available' : 'Upstream gone'}
      </button>
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

function UpdateDialog({ slug, onClose, onDone }: DialogProps) {
  const navigate = useNavigate()
  const status = useAsync(() => api.getUpstream(slug), [slug])
  const [busy, setBusy] = useState<'merge' | 'dismiss' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const preview = status.data?.state === 'update' ? status.data.preview : null

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
    <Dialog
      open
      title="Update available"
      description={
        status.data
          ? `${status.data.upstream.id} has changed since this copy was made or last updated.`
          : undefined
      }
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={() => void dismiss()} disabled={!preview || !!busy}>
            {busy === 'dismiss' && <Spinner />}
            Not now
          </Button>
          <Button variant="primary" onClick={() => void take()} disabled={!preview || !!busy}>
            {busy === 'merge' && <Spinner />}
            Take update
          </Button>
        </>
      }
    >
      {status.loading && (
        <p className="flex items-center gap-2 text-[13px] text-muted">
          <Spinner /> Working out the merge
        </p>
      )}
      {status.error && (
        <p role="alert" className="text-[13px] text-warn">
          Could not load the update: {status.error.message}
        </p>
      )}
      {status.data && !preview && (
        <p className="text-[13px] text-muted">There is no update to take any more.</p>
      )}
      {status.data && preview && <UpdatePreview status={status.data} preview={preview} />}
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}

function UpdatePreview({ status, preview }: { status: UpstreamStatus; preview: MergePreview }) {
  const { upstream, revision } = status
  const diff = useAsync(
    // The upstream's own history: what changed there between `base` and now.
    () => api.getVersionDiff(upstream.id, revision ?? '', upstream.base ?? undefined),
    [upstream.id, revision, upstream.base],
  )
  const conflicts = countConflicts(preview.merged)

  return (
    <div className="flex flex-col gap-4">
      <section>
        <h3 className="mb-1.5 text-[13px] font-medium">Upstream changes</h3>
        <div className="max-h-56 overflow-y-auto rounded-[6px] border border-line bg-surface-2">
          {diff.loading && (
            <p className="flex items-center gap-2 p-3 text-[12px] text-muted">
              <Spinner /> Loading the diff
            </p>
          )}
          {diff.error && (
            <p role="alert" className="p-3 text-[12px] text-warn">
              Could not load the diff: {diff.error.message}
            </p>
          )}
          {diff.data && <UnifiedDiff patch={diff.data.patch} />}
        </div>
      </section>

      <section>
        <h3 className="mb-1.5 text-[13px] font-medium">Merge result</h3>
        {preview.clean ? (
          <p data-testid="merge-verdict" className="mb-1.5 text-[12px] text-ok">
            Merges cleanly with your edits.
          </p>
        ) : (
          <p data-testid="merge-verdict" className="mb-1.5 text-[12px] text-warn">
            {conflicts === 1 ? '1 conflict' : `${conflicts} conflicts`} with your edits. Taking
            the update opens the marked-up source in the editor to resolve.
          </p>
        )}
        <pre
          data-testid="merge-result"
          className="sb-num max-h-56 overflow-auto rounded-[6px] border border-line bg-surface-2 p-3 text-[12px] leading-[1.5] whitespace-pre"
        >
          {preview.merged}
        </pre>
        {(preview.taken ?? []).length > 0 && (
          <p className="mt-1.5 text-[12px] text-muted">
            Also takes: <span className="sb-num">{(preview.taken ?? []).join(', ')}</span>
          </p>
        )}
        {(preview.kept ?? []).length > 0 && (
          <p className="mt-1.5 text-[12px] text-muted">
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
    <Dialog
      open
      title="Upstream gone"
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void detach()} disabled={busy}>
            {busy && <Spinner />}
            Detach
          </Button>
        </>
      }
    >
      <p className="text-[13px] text-muted">
        The template this one was duplicated from no longer exists, so there are no more
        updates to take. Detach it to keep this as an ordinary template of yours.
      </p>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}
