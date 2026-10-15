import type { ReactElement, ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { Button } from '@/components/ui/button'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Spinner } from '../ui/Spinner'

/**
 * #3653 — a flow's confirmation before a final step (archive, delete, forget, revoke,
 * reset): an alert dialog opened by `trigger`, which gets the focus back. The action is
 * the user's own (`USER_ONLY`) and keeps the dialog open while it runs, so a refusal
 * (`error`) is shown in it; Cancel and Escape do nothing meanwhile.
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
}) {
  return (
    <AlertDialog open={open} onOpenChange={(next) => (busy ? undefined : onOpenChange(next))}>
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent className="sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="flex flex-col gap-2 text-left">{children}</div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <Button variant={destructive ? 'destructive' : 'default'} onClick={onAction} disabled={busy} aria-busy={busy} {...USER_ONLY}>
            {busy && <Spinner />}
            {action}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
