import type { Access } from '../../api/capabilities'
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
import { lowerLine } from './access'

/**
 * Confirms choosing an access below the item's default: the whole guard on a lowering.
 * Cancel, Escape or a click outside calls `onCancel`, and nothing is sent. Keep it mounted
 * and drive `open`, so Radix's close (and `returnFocus`) runs.
 */
export function LowerConfirm({
  open,
  label,
  defaultAccess,
  from,
  to,
  onConfirm,
  onCancel,
  returnFocus,
}: {
  open: boolean
  label: string
  /** The item's default; `null` when it depends on the call. */
  defaultAccess: Access | null
  /** The access the row shows (`shownAccess`). */
  from: Access | 'default'
  to: Access
  onConfirm: () => void
  onCancel: () => void
  /** Where focus goes once the dialog has closed, either way (the row's select). */
  returnFocus?: () => void
}) {
  const line = lowerLine(defaultAccess, from, to)
  return (
    <AlertDialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <AlertDialogContent
        onCloseAutoFocus={(event) => {
          if (!returnFocus) return
          event.preventDefault()
          returnFocus()
        }}
      >
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">{`Lower ${label}?`}</AlertDialogTitle>
          {line !== null && <AlertDialogDescription>{line}</AlertDialogDescription>}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          {/* A plain button: an AlertDialogAction also closes the dialog, which is Cancel. */}
          <Button onClick={onConfirm}>{`Lower to ${to}`}</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
