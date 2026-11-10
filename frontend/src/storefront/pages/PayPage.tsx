import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { StoreError, errorText, store, type PayCheckout } from '../api/client'
import { formatMoney } from '../../lib/money'

/** What a link that opens no payment reads (an unknown, spent or replaced link). */
const NO_PAYMENT =
  'This link does not open a payment. It may be paid already or replaced by a newer one: use the link in the latest mail from the shop.'
const SDK_FAILED = 'PayPal could not be reached. Check your connection and reload this page.'

interface Buttons {
  render: (container: HTMLElement) => Promise<void>
  close: () => Promise<void> | void
}

interface PayPalSdk {
  Buttons: (options: {
    createOrder: () => Promise<string>
    onApprove: (data: { orderID: string }) => Promise<void>
    onError: (error: unknown) => void
  }) => Buttons
}

declare global {
  interface Window {
    paypal?: PayPalSdk
  }
}

/** Whether the payer came from this site's order page (`/o`; a referrer holds no fragment). */
function cameFromOrderPage(): boolean {
  try {
    const from = new URL(document.referrer)
    return from.origin === window.location.origin && from.pathname === '/o'
  } catch {
    return false
  }
}

/**
 * PayPal's JS SDK for this checkout, `enable-funding=venmo` so Venmo's button shows
 * (https://developer.paypal.com/sdk/js/configuration/). The script is loaded from
 * paypal.com for the live and the sandbox client id alike; the `/pay` page's CSP allows it
 * and no other page's does (`backend/scadbuddy/storefront/static.py`).
 */
function loadSdk(checkout: PayCheckout): { loaded: Promise<PayPalSdk>; cancel: () => void } {
  const script = document.createElement('script')
  const query = new URLSearchParams({
    'client-id': checkout.client_id,
    currency: checkout.currency,
    'enable-funding': 'venmo',
  })
  script.src = `https://www.paypal.com/sdk/js?${query.toString()}`
  script.async = true
  const loaded = new Promise<PayPalSdk>((resolve, reject) => {
    script.addEventListener('load', () => (window.paypal ? resolve(window.paypal) : reject(new Error('no sdk'))))
    script.addEventListener('error', () => reject(new Error('no sdk')))
  })
  document.head.appendChild(script)
  return {
    loaded,
    cancel: () => {
      script.remove()
      delete window.paypal
    },
  }
}

/**
 * `/pay#<token>`: pays one PayPal request, with Venmo, on PayPal's own buttons (spec §7,
 * #4960). The token is read from the URL fragment, which the browser never sends, and goes
 * to the shop in a body: never from the path, never into a path, a log or the history. An
 * approval says "thanks" only; the shop's webhook settles the payment (spec §7), and the
 * order page shows it paid.
 */
export function PayPage() {
  const navigate = useNavigate()
  const token = useLocation().hash.replace(/^#/, '')
  const [checkout, setCheckout] = useState<PayCheckout | null | undefined>(token ? undefined : null)
  const [error, setError] = useState<string | null>(null)
  const [approved, setApproved] = useState(false)
  const container = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!token) return
    let live = true
    store.payCheckout(token).then(
      (found) => live && setCheckout(found),
      (caught: unknown) => {
        if (!live) return
        if (caught instanceof StoreError && caught.status === 404) setCheckout(null)
        else setError(errorText(caught))
      },
    )
    return () => {
      live = false
    }
  }, [token])

  useEffect(() => {
    if (!checkout) return
    let live = true
    let buttons: Buttons | null = null
    const sdk = loadSdk(checkout)
    sdk.loaded.then(
      (paypal) => {
        if (!live || !container.current) return
        buttons = paypal.Buttons({
          createOrder: () => Promise.resolve(checkout.paypal_order_id),
          onApprove: () => {
            setApproved(true)
            return Promise.resolve()
          },
          onError: () => setError('PayPal could not take this payment. Try again in a moment.'),
        })
        void buttons.render(container.current)
      },
      () => live && setError(SDK_FAILED),
    )
    return () => {
      live = false
      sdk.cancel()
      void buttons?.close()
    }
  }, [checkout])

  if (checkout === null) {
    return (
      <section>
        <h1 className="sf-h1">Pay</h1>
        <div className="sf-panel">
          <p className="sf-lead">{NO_PAYMENT}</p>
        </div>
      </section>
    )
  }

  return (
    <section>
      <h1 className="sf-h1">Pay</h1>
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      {checkout === undefined && !error && <p className="sf-hint">Loading your payment…</p>}
      {checkout && (
        <div className="sf-panel">
          <p className="sf-lead">{formatMoney(checkout.amount_minor, checkout.currency, checkout.minor_exponent)}</p>
          <p className="sf-hint">{`For order ${checkout.order_number}`}</p>
          {approved ? (
            <>
              <p role="status">Thanks. We will confirm your payment shortly.</p>
              {cameFromOrderPage() ? (
                <button type="button" className="sf-button" onClick={() => navigate(-1)}>
                  Back to your order
                </button>
              ) : (
                <p className="sf-hint">Open your order from the link in your mail.</p>
              )}
            </>
          ) : (
            <div ref={container} />
          )}
        </div>
      )}
    </section>
  )
}
