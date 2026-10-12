import { useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { FlowDefinition, FlowSummary } from '../../api/types'
import { USER_ONLY } from '../../agent/dom'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

type Pending = 'archive' | 'delete' | null

/**
 * #2485 — archive, unarchive, delete or restore a flow, every version of it, from the
 * Flows tab and the flow page. Archiving hides the flow and stops new runs, and is
 * undone by Unarchive. Deleting is a commit that removes its script from the flows'
 * history, which keeps every version, so Restore brings it back as its next version.
 * Both confirm first and say what happens to runs; a delete's confirmation is the
 * user's own (an assistant's delete_flow waits for an approval instead).
 */
export function FlowActions({
  flow,
  onChanged,
  onRestored,
  restore,
}: {
  flow: Pick<FlowSummary, 'name' | 'archived_at' | 'deleted_at'>
  /** After an archive, unarchive or delete: read the flow again. */
  onChanged: () => void
  /** After a restore, with the new version. */
  onRestored: (restored: FlowDefinition) => void
  /** The version a deleted flow is restored to: its commit and number. */
  restore: { commit: string; version: number } | null
}) {
  const [pending, setPending] = useState<Pending>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  async function act(work: () => Promise<unknown>, after: (result: unknown) => void) {
    setBusy(true)
    setError(undefined)
    try {
      after(await work())
      setPending(null)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setBusy(false)
    }
  }

  const close = () => {
    if (busy) return
    setPending(null)
    setError(undefined)
  }

  const deleted = flow.deleted_at != null
  const archived = flow.archived_at != null

  return (
    <>
      <div className="flex flex-wrap gap-2">
        {deleted ? (
          restore && (
            <Button
              size="sm"
              disabled={busy}
              onClick={() =>
                void act(
                  () => api.restoreFlowVersion(flow.name, restore.commit),
                  (made) => onRestored(made as FlowDefinition),
                )
              }
            >
              {busy ? <Spinner /> : `Restore v${restore.version}`}
            </Button>
          )
        ) : archived ? (
          <Button size="sm" disabled={busy} onClick={() => void act(() => api.unarchiveFlow(flow.name), onChanged)}>
            {busy ? <Spinner /> : 'Unarchive'}
          </Button>
        ) : (
          <Button size="sm" onClick={() => setPending('archive')}>
            Archive
          </Button>
        )}
        {!deleted && (
          <Button size="sm" variant="danger" onClick={() => setPending('delete')}>
            Delete
          </Button>
        )}
      </div>
      {error && pending === null && (
        <p role="alert" className="mt-1 text-[12.5px] text-warn">
          {error}
        </p>
      )}
      <Dialog
        open={pending === 'archive'}
        title={`Archive ${flow.name}?`}
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void act(() => api.archiveFlow(flow.name), onChanged)} disabled={busy}>
              {busy ? <Spinner /> : 'Archive'}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 text-[13px] text-muted">
          <p>
            Every version of it leaves the Flows list (Show archived lists it again), and none of them can be started
            until you unarchive it.
          </p>
          <p>Runs in progress keep running. Finished runs keep their steps, their result and the script they ran.</p>
          {error && (
            <p role="alert" className="text-warn">
              {error}
            </p>
          )}
        </div>
      </Dialog>
      <Dialog
        open={pending === 'delete'}
        title={`Delete ${flow.name}?`}
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => void act(() => api.deleteFlow(flow.name), onChanged)}
              disabled={busy}
              {...USER_ONLY}
            >
              {busy ? <Spinner /> : 'Delete flow'}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 text-[13px] text-muted">
          <p>
            Its script is removed by a commit in the flows&apos; history, which keeps every version. No run of it can be
            started.
          </p>
          <p>
            Runs in progress keep running on the version they started with. Finished runs still show the script they
            ran.
          </p>
          <p>To bring it back, restore one of its versions: Show archived lists it.</p>
          {error && (
            <p role="alert" className="text-warn">
              {error}
            </p>
          )}
        </div>
      </Dialog>
    </>
  )
}

