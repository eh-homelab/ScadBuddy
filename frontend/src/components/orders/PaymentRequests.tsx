import { useState } from 'react'
import { api } from '../../api/client'
import type { Order, PaymentRequestView, ProviderStatus } from '../../api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { copyText } from '../../lib/clipboard'
import { formatMoney, toMajor, toMinor } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { When } from './When'
import { refusalText } from './refusal'

type Variant = 'default' | 'secondary' | 'outline' | 'destructive'
const STATUS: Record<PaymentRequestView['status'], { label: string; variant: Variant }> = {
  open: { label: 'Open', variant: 'secondary' },
  // #4958: a bank payment (US ACH) on its way, and one that did not settle.
  pending: { label: 'Pending', variant: 'secondary' },
  paid: { label: 'Paid', variant: 'default' },
  expired: { label: 'Expired', variant: 'outline' },
  cancelled: { label: 'Cancelled', variant: 'destructive' },
  failed: { label: 'Failed', variant: 'destructive' },
}
const ENDED = new Set<Order['status']>(['closed', 'cancelled'])

/**
 * #4957 — an order's payment requests: ask the customer for money through a provider's
 * hosted page or by hand. The link is shown only while the request is open: once it is
 * pending, paid, cancelled, expired or failed, no one should pay it. A pending request
 * (#4958) cannot be cancelled: the money is on its way. `balanceMinor` is the amount the
 * form starts from.
 */
export function PaymentRequests({
  order,
  requests,
  balanceMinor,
  onChanged,
}: {
  order: Order
  requests: PaymentRequestView[]
  balanceMinor: number
  onChanged: () => void
}) {
  const providers = useAsync(() => api.paymentProviders(), []).data
  const connected = (providers ?? []).filter((p) => p.connected)
  const [form, setForm] = useState<{ amount: string; kind: 'deposit' | 'balance'; provider: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState<{ id: string; ok: boolean } | null>(null)

  async function act(action: () => Promise<unknown>): Promise<boolean> {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
      return true
    } catch (err) {
      setError(refusalText(err))
      return false
    } finally {
      setBusy(false)
    }
  }

  function submit() {
    if (!form) return
    const typed = form.amount.trim()
    const minor = typed === '' ? undefined : toMinor(typed, order.minor_exponent)
    if (minor === null || minor === 0) {
      setError('Enter the amount to ask for, such as 12.50.')
      return
    }
    // With a provider connected the person chooses one, or by hand; with none the request is by hand.
    const provider = connected.length > 0 ? { provider: form.provider === NONE ? null : form.provider } : {}
    void act(() => api.requestPayment(order.id, { kind: form.kind, ...(minor === undefined ? {} : { amount_minor: minor }), ...provider })).then((ok) => {
      if (ok) setForm(null)
    })
  }

  const label = (id: string | null) => (id === null ? 'By hand' : (providers ?? []).find((p) => p.id === id)?.label ?? id)
  const money = (minor: number) => formatMoney(minor, order.currency, order.minor_exponent)

  return (
    <section aria-label="Payment requests" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[13px] font-medium">Payment requests</h3>
        {!ENDED.has(order.status) && !form && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setError(null)
              setForm({ amount: toMajor(Math.max(balanceMinor, 0), order.minor_exponent), kind: 'balance', provider: connected.find((p) => p.is_default)?.id ?? NONE })
            }}
          >
            Request payment
          </Button>
        )}
      </div>

      {form && (
        <form
          aria-label="Request payment"
          className="flex flex-wrap items-end gap-2 rounded-md border p-2"
          onSubmit={(event) => {
            event.preventDefault()
            submit()
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor="request-amount">Amount</Label>
            <Input id="request-amount" inputMode="decimal" className="w-28" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="request-kind">Kind</Label>
            <Select value={form.kind} onValueChange={(kind) => setForm({ ...form, kind: kind as 'deposit' | 'balance' })}>
              <SelectTrigger id="request-kind" aria-label="Kind" className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                <SelectItem value="balance">Balance</SelectItem>
                <SelectItem value="deposit">Deposit</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {connected.length > 0 && <ProviderSelect connected={connected} value={form.provider} onChange={(provider) => setForm({ ...form, provider })} />}
          <Button type="submit" size="sm" disabled={busy}>
            Request
          </Button>
          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setForm(null)}>
            Close
          </Button>
        </form>
      )}

      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}

      {requests.length === 0 ? (
        <p className="text-xs text-muted-foreground">No payment requests yet.</p>
      ) : (
        <ul aria-label="Payment requests" className="flex flex-col divide-y rounded-md border text-[13px]">
          {requests.map((r) => (
            <li key={r.id} className="flex flex-col gap-1 px-3 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="capitalize">{r.kind}</span>
                  <Badge variant={STATUS[r.status].variant} className="font-normal">
                    {STATUS[r.status].label}
                  </Badge>
                  <span className="text-muted-foreground">{label(r.provider)}</span>
                  <span className="text-xs text-muted-foreground">
                    <When at={r.paid_at ?? r.created_at} />
                  </span>
                </span>
                <span className="sb-num">{money(r.amount_minor)}</span>
              </div>
              {r.status === 'pending' && <span className="text-xs text-muted-foreground">The bank payment is on its way</span>}
              {/* #5700: the provider's page may still take money until the provider confirms the close; once the sweep gives up (a provider not connected), the shop closes it there. */}
              {r.close_abandoned_at ? (
                <span className="text-xs text-warn">{`Could not close; close it at ${label(r.provider)}`}</span>
              ) : (
                r.close_pending_at && <span className="text-xs text-warn">{`Close not yet confirmed by ${label(r.provider)}; ScadBuddy tries again`}</span>
              )}
              {r.status === 'open' && (
                <div className="flex flex-wrap items-center gap-2">
                  {r.hosted_url ? (
                    <>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={(event) => {
                          const link = event.currentTarget.parentElement?.querySelector<HTMLElement>('[data-payment-link]')
                          void copyText(r.hosted_url ?? '', link).then((ok) => setCopied({ id: r.id, ok }))
                        }}
                      >
                        Copy link
                      </Button>
                      <Button size="sm" variant="outline" asChild>
                        <a href={`mailto:?subject=${encodeURIComponent(`Payment for order ${order.number}`)}&body=${encodeURIComponent(r.hosted_url)}`}>Email</a>
                      </Button>
                      <code data-payment-link className="text-xs break-all text-muted-foreground">
                        {r.hosted_url}
                      </code>
                      {copied?.id === r.id && (
                        <span role="status" className={copied.ok ? 'text-xs text-muted-foreground' : 'text-xs text-warn'}>
                          {copied.ok ? 'Copied' : 'Copy failed, press Ctrl+C'}
                        </span>
                      )}
                    </>
                  ) : r.link_error ? (
                    <>
                      <span className="text-xs break-words text-warn">{r.link_error}</span>
                      <Button size="sm" variant="outline" disabled={busy} onClick={() => void act(() => api.linkPaymentRequest(order.id, r.id))}>
                        Retry link
                      </Button>
                    </>
                  ) : (
                    <span className="text-xs text-muted-foreground">Record the payment below when it arrives</span>
                  )}
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act(() => api.cancelPaymentRequest(order.id, r.id))}>
                    Cancel
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

const NONE = '__hand__'

function ProviderSelect({ connected, value, onChange }: { connected: ProviderStatus[]; value: string; onChange: (id: string) => void }) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor="request-provider">Provider</Label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id="request-provider" aria-label="Provider" className="w-36">
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper">
          {connected.map((p) => (
            <SelectItem key={p.id} value={p.id}>
              {p.label}
            </SelectItem>
          ))}
          <SelectItem value={NONE}>By hand</SelectItem>
        </SelectContent>
      </Select>
    </div>
  )
}
