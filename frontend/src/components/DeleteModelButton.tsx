import { useState } from 'react'
import { useNavigate } from 'react-router'
import { ApiError, api } from '../api/client'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  slug: string
  name: string
}

/**
 * Deletes the model after a confirm, then goes back to the catalogue, which refetches on
 * mount. One that duplicates track is refused (409) until confirmed again (#157).
 */
export function DeleteModelButton({ slug, name }: Props) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** How many duplicates track this template, once the server has refused for them. */
  const [tracked, setTracked] = useState<number | null>(null)

  function close() {
    if (deleting) return
    setOpen(false)
    setError(null)
    setTracked(null)
  }

  async function confirm() {
    setDeleting(true)
    setError(null)
    try {
      await api.deleteModel(slug, tracked !== null)
      navigate('/', { replace: true })
    } catch (caught) {
      const duplicates = caught instanceof ApiError ? caught.problem['duplicates'] : undefined
      if (caught instanceof ApiError && caught.status === 409 && typeof duplicates === 'number') {
        setTracked(duplicates)
      } else {
        setError(caught instanceof ApiError ? caught.detail : String(caught))
      }
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
              {deleting ? <Spinner /> : tracked !== null ? 'Delete anyway' : 'Delete model'}
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">
          <span className="font-medium text-ink">{name}</span> (<code>{slug}</code>) leaves the
          catalogue, along with the 3MFs saved for it. Files already sent to Bambuddy stay there,
          and the model&apos;s revisions stay in the history.
        </p>
        {tracked !== null && (
          <p role="alert" className="mt-3 text-[13px] text-warn">
            {tracked === 1 ? '1 template is a duplicate' : `${tracked} templates are duplicates`} of
            this one. They keep their source, but can no longer take its updates.
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
