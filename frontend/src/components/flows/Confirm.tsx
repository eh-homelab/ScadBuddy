import type { ReactElement, ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Spinner } from '../ui/spinner'

/**
 * #3653 — a flow's confirmation before a final step (archive, delete, forget, revoke,
 * reset): an `alertdialog` opened by `trigger`, which gets the focus back. Built on the
 * companion-aware dialog, not Radix's AlertDialog, which is always modal: the agent can
 * open one, and the assistant panel must stay usable beside it (#798). The action keeps
 * the dialog open while it runs, so a refusal (`error`) is shown in it; Cancel and Escape
 * do nothing meanwhile. The action is the user's own (`USER_ONLY`) unless `userOnly` is
 * false (Archive, which the agent may confirm as before).
 */
export function Confirm({
  trigger,
  open,
  onOpenChange,
  title,
  children,
  action,
  onAction,
  busy,
  error,
  destructive = true,
  userOnly = true,
}: {
  trigger: ReactElement
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  children: ReactNode
  action: string
  onAction: () => void
  busy: boolean
  error?: string
  destructive?: boolean
  userOnly?: boolean
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => (busy ? undefined : onOpenChange(next))}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent
        role="alertdialog"
        showCloseButton={false}
        className="sm:max-w-md"
        // An alert dialog is answered, not dismissed by a press beside it.
        onInteractOutside={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription asChild>
            <div className="flex flex-col gap-2 text-left">{children}</div>
          </DialogDescription>
        </DialogHeader>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline" disabled={busy}>
              Cancel
            </Button>
          </DialogClose>
          <Button
            variant={destructive ? 'destructive' : 'default'}
            onClick={onAction}
            disabled={busy}
            aria-busy={busy}
            {...(userOnly ? USER_ONLY : {})}
          >
            {busy && <Spinner />}
            {action}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
