import { useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { FlowDefinition, FlowSummary } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Spinner } from '../ui/spinner'
import { Confirm } from './Confirm'

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

  const confirm = (kind: Exclude<Pending, null>) => (open: boolean) => {
    if (open) setPending(kind)
    else close()
  }

  return (
    <>
      <div className="flex flex-wrap gap-2">
        {deleted ? (
          restore && (
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              aria-busy={busy}
              onClick={() =>
                void act(
                  () => api.restoreFlowVersion(flow.name, restore.commit),
                  (made) => onRestored(made as FlowDefinition),
                )
              }
            >
              {busy && <Spinner />}
              {`Restore v${restore.version}`}
            </Button>
          )
        ) : archived ? (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            aria-busy={busy}
            onClick={() => void act(() => api.unarchiveFlow(flow.name), onChanged)}
          >
            {busy && <Spinner />}
            Unarchive
          </Button>
        ) : (
          <Confirm
            trigger={
              <Button variant="outline" size="sm">
                Archive
              </Button>
            }
            open={pending === 'archive'}
            onOpenChange={confirm('archive')}
            title={`Archive ${flow.name}?`}
            action="Archive"
            destructive={false}
            userOnly={false}
            busy={busy}
            error={error}
            onAction={() => void act(() => api.archiveFlow(flow.name), onChanged)}
          >
            <p>
              Every version of it leaves the Flows list (Show archived lists it again), and none of them can be started
              until you unarchive it.
            </p>
            <p>Runs in progress keep running. Finished runs keep their steps, their result and the script they ran.</p>
          </Confirm>
        )}
        {!deleted && (
          <Confirm
            trigger={
              <Button variant="destructive" size="sm">
                Delete
              </Button>
            }
            open={pending === 'delete'}
            onOpenChange={confirm('delete')}
            title={`Delete ${flow.name}?`}
            action="Delete flow"
            busy={busy}
            error={error}
            onAction={() => void act(() => api.deleteFlow(flow.name), onChanged)}
          >
            <p>
              Its script is removed by a commit in the flows&apos; history, which keeps every version. No run of it can
              be started.
            </p>
            <p>
              Runs in progress keep running on the version they started with. Finished runs still show the script they
              ran.
            </p>
            <p>To bring it back, restore one of its versions: Show archived lists it.</p>
          </Confirm>
        )}
      </div>
      {error && pending === null && (
        <p role="alert" className="mt-1 text-[12.5px] text-destructive">
          {error}
        </p>
      )}
    </>
  )
}
