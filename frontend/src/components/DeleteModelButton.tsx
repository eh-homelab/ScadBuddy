import { useState } from 'react'
import { useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import { trackingDuplicates } from '../lib/problems'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

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

  function close() {
    if (deleting) return
    setOpen(false)
    setError(null)
    setDuplicates(null)
  }

  async function confirm() {
    setDeleting(true)
    setError(null)
    try {
      await api.deleteModel(slug, duplicates !== null)
      navigate('/', { replace: true })
    } catch (caught) {
      const tracking = caught instanceof ApiError ? trackingDuplicates(caught.problem) : undefined
      if (tracking) setDuplicates(tracking)
      else setError(caught instanceof ApiError ? caught.detail : String(caught))
      setDeleting(false)
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
            <Button variant="ghost" onClick={close} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void confirm()} disabled={deleting}>
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
        {error && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {error}
          </p>
        )}
      </Dialog>
    </>
  )
}
