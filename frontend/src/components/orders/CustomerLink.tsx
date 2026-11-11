import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { refusalSlug, refusalText } from './refusal'
import { When } from './When'

/**
 * #5283 (storefront spec §5.2 "Order link") — the link that opens this order on the storefront
 * without an account. "Send link" mails one to the address typed, or else to the customer's
 * email contact; with a live link the button reads "Send a new link" and asks first, since the
 * old link stops working. "Revoke" ends the live link. The card shows the link's state only:
 * the link itself goes to the customer by mail and never to this page. Everything shown is text.
 * The server enforces the confirm: a send replaces a live link only with `replace` true, else it
 * is 409 `link_live`. "Send link" sends with `replace` false; on `link_live` (this page's state
 * was stale) the card asks the same question, and "Yes" sends again with `replace` true. Until
 * the state is read, "Send link" is disabled. A failed read says so and offers "Try again".
 */
export function CustomerLink({ orderId, onChanged }: { orderId: string; onChanged?: () => void }) {
  const link = useAsync(() => api.customerLink(orderId), [orderId], [`order:${orderId}`])
  const [to, setTo] = useState('')
  const [confirming, setConfirming] = useState(false)
  // The address of the live link a `link_live` refusal's fresh read found.
  const [liveTo, setLiveTo] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const state = link.data

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setConfirming(false)
      setTo('')
      onChanged?.()
    } catch (err) {
      if (refusalSlug(err) === 'link_live') {
        // The page's state was stale. Ask only if a link is still live after a fresh read.
        const fresh = await api.customerLink(orderId).catch(() => null)
        setLiveTo(fresh?.live ? fresh.sent_to : null)
        setConfirming(fresh?.live !== false)
      } else setError(refusalText(err))
    } finally {
      link.refresh()
      setBusy(false)
    }
  }

  const send = (replace: boolean) => void act(() => api.sendCustomerLink(orderId, to.trim() || undefined, replace))

  return (
    <section className="flex flex-col gap-2" aria-label="Customer link">
      <h2 className="text-sm font-medium">Customer link</h2>
      {!state && link.error && (
        <div className="flex flex-col gap-1 text-[13px]">
          <p role="alert" className="text-destructive">
            The link's state could not be read: {link.error.message}
          </p>
          <p className="text-muted-foreground">Sending waits until the state is read.</p>
          <Button size="sm" variant="outline" className="self-start" onClick={() => link.refresh()}>
            Try again
          </Button>
        </div>
      )}
      {state && (
        <div className="flex flex-col gap-0.5 text-[13px]">
          {state.sent_to === null ? (
            <p className="text-muted-foreground">No link sent yet.</p>
          ) : state.live ? (
            <>
              <p>
                Live: sent to <span className="break-all">{state.sent_to}</span>
                {state.created_at && (
                  <>
                    {' · '}
                    <When at={state.created_at} />
                  </>
                )}
              </p>
              {state.opened_at ? (
                <p className="text-muted-foreground">
                  Opened <When at={state.opened_at} />
                </p>
              ) : (
                <p className="text-muted-foreground">Not opened yet.</p>
              )}
            </>
          ) : (
            <p className="text-muted-foreground">
              Revoked {state.revoked_at && <When at={state.revoked_at} />} (last sent to <span className="break-all">{state.sent_to}</span>)
            </p>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`customer-link-to-${orderId}`}>Send to</Label>
          <Input
            id={`customer-link-to-${orderId}`}
            type="email"
            className="w-64"
            placeholder="The customer's email"
            value={to}
            disabled={busy}
            onChange={(e) => setTo(e.target.value)}
          />
        </div>
        {state?.live ? (
          <Button size="sm" variant="outline" disabled={busy || confirming} onClick={() => setConfirming(true)} {...USER_ONLY}>
            Send a new link
          </Button>
        ) : (
          <Button size="sm" variant="outline" disabled={busy || !state} onClick={() => send(false)} {...USER_ONLY}>
            Send link
          </Button>
        )}
        {state?.live && (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act(() => api.revokeCustomerLink(orderId))}>
            Revoke
          </Button>
        )}
      </div>
      {confirming && (
        <div className="flex flex-col gap-2 rounded-md border p-2 text-[13px]">
          <p>
            {(state?.live ? state.sent_to : liveTo) ? (
              <>
                The link sent to <span className="break-all">{state?.live ? state.sent_to : liveTo}</span> stops working.
              </>
            ) : (
              <>The live link stops working.</>
            )}{' '}
            The customer gets a new one by mail.
          </p>
          <span className="flex gap-2">
            <Button size="sm" disabled={busy} onClick={() => send(true)} {...USER_ONLY}>
              Yes, send a new link
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </span>
        </div>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
