import { useState } from 'react'
import { useNavigate } from 'react-router'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import { trackingDuplicates } from '../lib/problems'
import { Spinner } from './ui/spinner'
import { Trash2Icon } from 'lucide-react'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'

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
      setDuplicates(tracking ?? null)
      if (!tracking) setError(caught instanceof ApiError ? caught.detail : String(caught))
      setDeleting(false)
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => setOpen(true)}
        className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
      >
        <Trash2Icon /> Delete
      </Button>
      <AlertDialog open={open} onOpenChange={(next) => !next && close()}>
        <AlertDialogContent className="data-[size=default]:sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle>{`Delete ${name}?`}</AlertDialogTitle>
            <AlertDialogDescription>
              <span className="font-medium text-foreground">{name}</span> (<code>{slug}</code>) leaves the catalogue,
              along with the 3MFs saved for it. Files already sent to Bambuddy stay there, and the model&apos;s revisions
              stay in the history.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {duplicates && (
            <div role="alert" className="text-sm text-destructive">
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
              <p className="mt-1">Deleting it anyway leaves them as they are, with an upstream that is gone.</p>
            </div>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <Button variant="destructive" onClick={() => void confirm()} disabled={deleting} {...USER_ONLY}>
              {deleting ? <Spinner /> : duplicates ? 'Delete anyway' : 'Delete model'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
