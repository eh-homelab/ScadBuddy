import { useState } from 'react'
import { api } from '../../api/client'
import type { OrderItemDetail } from '../../api/types'
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
import { Spinner } from '@/components/ui/spinner'
import { refusalText } from './refusal'

/**
 * The confirm step of Unfulfil: the item's latest accepted attempt stops counting as
 * made, and the item goes back to approved. The attempt stays on the record.
 */
export function UnfulfilDialog({
  orderId,
  item,
  open,
  onOpenChange,
  onChanged,
}: {
  orderId: string
  item: Pick<OrderItemDetail, 'id' | 'title'>
  open: boolean
  onOpenChange: (open: boolean) => void
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      await api.unfulfilOrderItem(orderId, item.id)
      onOpenChange(false)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return
        if (!next) setError(null)
        onOpenChange(next)
      }}
    >
      <AlertDialogContent size="sm">
        <AlertDialogHeader>
          <AlertDialogTitle>{`Unfulfil ${item.title}?`}</AlertDialogTitle>
          <AlertDialogDescription>
            The latest accepted attempt stops counting as made, and the item goes back to approved. The attempt stays on the
            record, and you can accept it again. The order's history records the change.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <Button variant="destructive" disabled={busy} onClick={() => void confirm()}>
            {busy && <Spinner />}
            Unfulfil
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
