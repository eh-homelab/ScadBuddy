import { useState } from 'react'
import { api } from '../../api/client'
import type { Order, OrderTotals, PaymentRequestView } from '../../api/types'
import { formatMoney, toMinor } from '../../lib/money'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'
import { Textarea } from '@/components/ui/textarea'
import { isCart, OPEN_FOR_EDITS, TAKES_ITEMS } from './afterAcceptance'
import { refusalText } from './refusal'

/** The backend's bound on a waiver's reason (`api/order_payments.py` `WaiveBody`). */
const REASON_MAX = 500

/**
 * A step that goes on without money the order asks for: it says what happens and takes a
 * reason (1 to 500 characters) for the audit line. The confirm stays off until one is typed.
 */
export function ReasonDialog({
  open,
  title,
  description,
  confirm,
  onOpenChange,
  onConfirm,
}: {
  open: boolean
  title: string
  description: string
  confirm: string
  onOpenChange: (open: boolean) => void
  /** Sends the trimmed reason; a refusal is shown in the dialog. */
  onConfirm: (reason: string) => Promise<unknown>
}) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const trimmed = reason.trim()

  async function go() {
    setBusy(true)
    setError(null)
    try {
      await onConfirm(trimmed)
      setReason('')
      onOpenChange(false)
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
        if (!next) {
          setError(null)
          setReason('')
        }
        onOpenChange(next)
      }}
    >
      <AlertDialogContent size="sm">
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <div className="flex flex-col gap-1">
          <Label htmlFor="waiver-reason">Reason</Label>
          <Textarea id="waiver-reason" rows={2} maxLength={REASON_MAX} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <Button disabled={busy || trimmed.length === 0} onClick={() => void go()}>
            {busy && <Spinner />}
            {confirm}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** "Ship without full payment": the dialog, opened by the caller's button. */
export function ShipUnpaidDialog({ orderId, open, onOpenChange, onChanged }: { orderId: string; open: boolean; onOpenChange: (open: boolean) => void; onChanged: () => void }) {
  return (
    <ReasonDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Ship without full payment?"
      description="The order ships with its balance unpaid. The balance request stays open, so the customer can still pay it. The order's history records the reason."
      confirm="Ship unpaid"
      onConfirm={(reason) => api.waiveBalance(orderId, reason).then(() => onChanged())}
    />
  )
}

/**
 * #5281 (storefront spec §8-§9) — the order's deposit and its balance. While draft or quoted
 * the deposit is set, cleared or taken from the shop's policy; until it is paid or waived
 * ("Proceed without deposit") a design is not sent and a test print is not claimed. Once
 * accepted, an unpaid balance holds the shipment (`shipping_held`) until it is paid or waived
 * ("Ship without full payment"). Each waiver takes a reason.
 */
export function DepositControls({
  order,
  totals,
  requests,
  onChanged,
}: {
  order: Order
  totals: OrderTotals
  requests: PaymentRequestView[]
  onChanged: () => void
}) {
  const { currency, minor_exponent: exponent } = order
  const money = (minor: number) => formatMoney(minor, currency, exponent)
  const [amount, setAmount] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [waiving, setWaiving] = useState<'deposit' | 'balance' | null>(null)
  const ended = order.status === 'closed' || order.status === 'cancelled'
  const editable = OPEN_FOR_EDITS.has(order.status) && !isCart(order)
  // The backend refuses a change while a deposit payment is on its way (`payment_in_flight`).
  const inFlight = requests.some((r) => r.kind === 'deposit' && r.status === 'pending')
  const deposit = order.deposit_minor ?? null
  const due = totals.deposit_due_minor ?? 0
  const held = totals.shipping_held ?? false

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setAmount('')
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  let state: string
  if (deposit === null) state = 'No deposit.'
  else if (order.deposit_waived_reason) state = `Deposit ${money(deposit)}. Waived: ${order.deposit_waived_reason}`
  else if (due > 0) state = `Deposit due ${money(due)}`
  else state = `Deposit ${money(deposit)} paid.`

  return (
    <section className="flex flex-col gap-2" aria-label="Deposit and balance">
      <h3 className="text-xs text-muted-foreground">Deposit</h3>
      <p className="text-[13px]" data-testid="deposit-state">
        {state}
      </p>
      {inFlight && <p className="text-xs text-muted-foreground">A deposit payment is on its way; the deposit can change once it settles.</p>}
      {editable && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            const minor = toMinor(amount, exponent)
            if (minor === null || minor < 1) {
              setError('Enter the deposit as an amount above zero, such as 20.00.')
              return
            }
            void run(() => api.setDeposit(order.id, minor))
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor="deposit-amount">{`Deposit (${currency})`}</Label>
            <Input id="deposit-amount" inputMode="decimal" className="w-28" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </div>
          <Button type="submit" size="sm" variant="outline" disabled={busy || inFlight}>
            Set deposit
          </Button>
          {deposit !== null && (
            <Button type="button" size="sm" variant="ghost" disabled={busy || inFlight} onClick={() => void run(() => api.setDeposit(order.id, null))}>
              Clear deposit
            </Button>
          )}
          <Button type="button" size="sm" variant="ghost" disabled={busy || inFlight} onClick={() => void run(() => api.applyDepositPolicy(order.id))}>
            Use the policy
          </Button>
        </form>
      )}
      {!ended && deposit !== null && !order.deposit_waived_reason && due > 0 && !isCart(order) && (
        <div>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setWaiving('deposit')}>
            Proceed without deposit
          </Button>
        </div>
      )}
      {order.balance_waived_reason && <p className="text-[13px]">Ships without full payment: {order.balance_waived_reason}</p>}
      {/* Once shipped there is no shipment left to hold. */}
      {!ended && TAKES_ITEMS.has(order.status) && held && (
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-[13px] text-warn">The unpaid balance holds the shipment.</p>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setWaiving('balance')}>
            Ship without full payment
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      <ReasonDialog
        open={waiving === 'deposit'}
        onOpenChange={(open) => setWaiving(open ? 'deposit' : null)}
        title="Proceed without the deposit?"
        description="The work goes on before the deposit is paid: designs can be sent and test prints made. The deposit request stays open. The order's history records the reason."
        confirm="Proceed"
        onConfirm={(reason) => api.waiveDeposit(order.id, reason).then(() => onChanged())}
      />
      <ShipUnpaidDialog orderId={order.id} open={waiving === 'balance'} onOpenChange={(open) => setWaiving(open ? 'balance' : null)} onChanged={onChanged} />
    </section>
  )
}
