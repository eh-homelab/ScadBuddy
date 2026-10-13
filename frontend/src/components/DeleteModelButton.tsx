import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import { renderInProgress, trackingDuplicates } from '../lib/problems'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

/**
 * How often a delete refused for a render in progress is sent again, and for how long
 * (#2387). The render is most often the preview this page started on opening the model,
 * which ends in seconds to minutes; past the limit the dialog shows the refusal.
 */
export const DELETE_RETRY_MS = 2_000
export const DELETE_WAIT_LIMIT_MS = 5 * 60_000

interface Props {
  slug: string
  name: string
}

/** Deletes the model after a confirm, then goes back to the catalogue, which refetches on mount. */
export function DeleteModelButton({ slug, name }: Props) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set when the server refused because these duplicates track the model; the confirm
  // then deletes anyway, leaving them with an upstream that is gone.
  const [duplicates, setDuplicates] = useState<string[] | null>(null)
  // Set while a delete refused for a render in progress waits to be sent again; Cancel
  // (or leaving the page) stops it.
  const [waiting, setWaiting] = useState(false)
  const attempt = useRef(0)
  useEffect(
    () => () => {
      attempt.current += 1
    },
    [],
  )

  function close() {
    if (deleting && !waiting) return
    // Stops a waiting delete: its next send finds a newer attempt and gives up.
    attempt.current += 1
    setOpen(false)
    setDeleting(false)
    setWaiting(false)
    setError(null)
    setDuplicates(null)
  }

  async function confirm() {
    const mine = ++attempt.current
    const force = duplicates !== null
    const deadline = Date.now() + DELETE_WAIT_LIMIT_MS
    setDeleting(true)
    setError(null)
    for (;;) {
      try {
        await api.deleteModel(slug, force)
        if (mine === attempt.current) navigate('/', { replace: true })
        return
      } catch (caught) {
        if (mine !== attempt.current) return
        const problem = caught instanceof ApiError ? caught.problem : undefined
        if (problem && renderInProgress(problem) && Date.now() < deadline) {
          setWaiting(true)
          await new Promise((resolve) => setTimeout(resolve, DELETE_RETRY_MS))
          if (mine !== attempt.current) return
          continue
        }
        const tracking = problem ? trackingDuplicates(problem) : undefined
        setDuplicates(tracking ?? null)
        if (!tracking) setError(caught instanceof ApiError ? caught.detail : String(caught))
        setWaiting(false)
        setDeleting(false)
        return
      }
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-warn/10 hover:text-warn"
      >
        Delete
      </button>
      <Dialog
        open={open}
        title={`Delete ${name}?`}
        onClose={close}
        footer={
          <>
            <Button variant="ghost" onClick={close} disabled={deleting && !waiting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void confirm()} disabled={deleting} {...USER_ONLY}>
              {deleting ? <Spinner /> : duplicates ? 'Delete anyway' : 'Delete model'}
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">
          <span className="font-medium text-ink">{name}</span> (<code>{slug}</code>) leaves the
          catalogue, along with the 3MFs saved for it. Files already sent to Bambuddy stay there,
          and the model&apos;s revisions stay in the history.
        </p>
        {duplicates && (
          <div role="alert" className="mt-3 text-[13px] text-warn">
            <p>
              {duplicates.length === 1
                ? '1 template is a duplicate of this one:'
                : `${duplicates.length} templates are duplicates of this one:`}
            </p>
            <ul className="mt-1 list-disc pl-5">
              {duplicates.map((duplicate) => (
                <li key={duplicate}>
                  <code>{duplicate}</code>
                </li>
              ))}
            </ul>
            <p className="mt-1">
              Deleting it anyway leaves them as they are, with an upstream that is gone.
            </p>
          </div>
        )}
        {waiting && (
          <p role="status" className="mt-3 text-[13px] text-muted">
            A render of this model is still running (most often its preview). Waiting for it
            to finish, then deleting.
          </p>
        )}
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </>
  )
}
