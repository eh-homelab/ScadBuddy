import { useId, useState, type FormEvent } from 'react'
import { api } from '../../api/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'
import { Textarea } from '@/components/ui/textarea'
import { refusalText } from './refusal'

/** The longest reason the route keeps (`AcceptForCustomerBody.reason`). */
const REASON_MAX = 500

/**
 * #5283 (storefront spec §6.1) — on a storefront order only the customer accepts a
 * revision, a quote or a price. The shop's stand-in is "Accept for the customer…": a
 * dialog that asks why the customer did not accept themselves and keeps that reason in
 * the order's history (`POST …/accept-for-customer`). It replaces the shop's own Accept
 * on such an order.
 */
export function AcceptForCustomerButton({
  orderId,
  itemId,
  what,
  targetId,
  disabled = false,
  title,
  onChanged,
}: {
  orderId: string
  itemId: string
  what: 'revision' | 'quote'
  targetId: string
  disabled?: boolean
  /** Why the button is off, when it is. */
  title?: string
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const formId = useId()
  const fieldId = useId()

  function close() {
    if (busy) return
    setOpen(false)
    setError(null)
  }

  async function accept(event: FormEvent) {
    event.preventDefault()
    const why = reason.trim()
    if (!why || busy) return
    setBusy(true)
    setError(null)
    try {
      await api.acceptForCustomer(orderId, itemId, { what, target_id: targetId, reason: why })
      setBusy(false)
      setOpen(false)
      setReason('')
      onChanged()
    } catch (caught) {
      setError(refusalText(caught))
      setBusy(false)
    }
  }

  return (
    <>
      <Button
        size="xs"
        variant="outline"
        disabled={disabled}
        title={title}
        onClick={() => {
          setError(null)
          setOpen(true)
        }}
      >
        Accept for the customer…
      </Button>
      <Dialog open={open} onOpenChange={(next) => !next && close()}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Accept for the customer</DialogTitle>
            <DialogDescription>
              This order came from the storefront, where the customer accepts the {what === 'revision' ? 'design' : 'price'} themselves. Say why you accept it for them. The reason is kept in the order's history.
            </DialogDescription>
          </DialogHeader>
          <form id={formId} className="grid gap-2" onSubmit={(event) => void accept(event)}>
            <Label htmlFor={fieldId}>Reason</Label>
            <Textarea id={fieldId} rows={2} maxLength={REASON_MAX} placeholder="By phone, 9 Nov" value={reason} onChange={(event) => setReason(event.target.value)} />
          </form>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" form={formId} disabled={busy || !reason.trim()}>
              {busy && <Spinner />}
              Accept for the customer
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
