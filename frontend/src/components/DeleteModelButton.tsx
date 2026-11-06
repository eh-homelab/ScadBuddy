import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import { renderInProgress, trackingDuplicates } from '../lib/problems'
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
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

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
  /**
   * #5422 — whether Settings' `library_backend` is ScadBuddy's own library, the only one
   * `delete_inbox_copies` acts on. False (or not yet known) leaves the choice out.
   */
  nativeLibrary: boolean
}

/** Deletes the model after a confirm, then goes back to the catalogue, which refetches on mount. */
export function DeleteModelButton({ slug, name, nativeLibrary }: Props) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set when the server refused because these duplicates track the model; the confirm
  // then deletes anyway, leaving them with an upstream that is gone.
  const [duplicates, setDuplicates] = useState<string[] | null>(null)
  // #5371 — the output delete's choice: also trash the outputs' copies in ScadBuddy's own
  // library. Offered on that library only (#5422); Bambuddy's inbox is never touched by a
  // model delete.
  const [alsoInbox, setAlsoInbox] = useState(false)
  // Set while a delete refused for a render in progress waits to be sent again; Cancel
  // (or leaving the page) stops it.
  const [waiting, setWaiting] = useState(false)
  // Set while a send is in flight. Cancel is offered only between sends: a send it
  // abandoned could still delete the model and leave this page on one that is gone.
  const [sending, setSending] = useState(false)
  const cancellable = !deleting || (waiting && !sending)
  const attempt = useRef(0)
  useEffect(
    () => () => {
      attempt.current += 1
    },
    [],
  )

  function close() {
    if (!cancellable) return
    // Stops a waiting delete: its next send finds a newer attempt and gives up.
    attempt.current += 1
    setOpen(false)
    setDeleting(false)
    setWaiting(false)
    setSending(false)
    setError(null)
    setDuplicates(null)
    setAlsoInbox(false)
  }

  async function confirm() {
    const mine = ++attempt.current
    const force = duplicates !== null
    const deadline = Date.now() + DELETE_WAIT_LIMIT_MS
    setDeleting(true)
    setError(null)
    for (;;) {
      setSending(true)
      try {
        await api.deleteModel(slug, force, nativeLibrary && alsoInbox)
        if (mine === attempt.current) navigate('/', { replace: true })
        return
      } catch (caught) {
        if (mine !== attempt.current) return
        setSending(false)
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

  const alsoId = `also-inbox-${slug}`
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
          {nativeLibrary && (
            <div className="flex flex-col gap-2 text-sm">
              <div className="flex items-center gap-2">
                <Checkbox
                  id={alsoId}
                  checked={alsoInbox}
                  disabled={deleting}
                  onCheckedChange={(next) => setAlsoInbox(next === true)}
                />
                <Label htmlFor={alsoId} className="font-normal">
                  Also delete the inbox copies in ScadBuddy&apos;s library
                </Label>
              </div>
              <p className="text-xs text-muted-foreground">
                Each output&apos;s copy still in this template&apos;s folder goes to the trash, with its per-print
                copies and their sliced files. Copies in Bambuddy&apos;s inbox are never deleted from here; delete an
                output on its own for those.
              </p>
            </div>
          )}
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
          {waiting && (
            <p role="status" className="text-sm text-muted-foreground">
              A render of this model is still running (most often its preview). Waiting for it to finish, then
              deleting.
            </p>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={!cancellable}>Cancel</AlertDialogCancel>
            <Button variant="destructive" onClick={() => void confirm()} disabled={deleting} {...USER_ONLY}>
              {deleting ? <Spinner /> : duplicates ? 'Delete anyway' : 'Delete model'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
