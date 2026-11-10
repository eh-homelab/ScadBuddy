import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { ACCOUNT_ORDER_URL, StoreError, errorText, store, type Account, type AccountOrder } from '../api/client'
import { useDraftChanged } from '../draftCount'

/** How a date reads on "Your orders": the visitor's own locale, the day only. */
function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/**
 * What a customer reads for each order status (backend `orders/models.py` `OrderStatus`).
 * A submitted order not yet quoted is still `draft`. A status this map lacks reads
 * "In progress", never the internal name.
 */
const STATUS_LABELS: Record<string, string> = {
  draft: 'Received',
  quoted: 'Quote ready',
  accepted: 'Accepted',
  in_production: 'Being made',
  ready: 'Ready',
  shipped: 'Shipped',
  closed: 'Complete',
  cancelled: 'Cancelled',
}

function statusLabel(status: string): string {
  return (Object.hasOwn(STATUS_LABELS, status) && STATUS_LABELS[status]) || 'In progress'
}

function OrderRow({ order }: { order: AccountOrder }) {
  const placed = order.submitted_at ?? order.created_at
  return (
    <li className="sf-item">
      <Link className="sf-link" to={ACCOUNT_ORDER_URL(order.id)}>
        {order.number ?? 'Your order'}
      </Link>
      <p className="sf-hint">
        {`${day(placed)} · ${order.items} ${order.items === 1 ? 'item' : 'items'} · ${statusLabel(order.status)}`}
      </p>
    </li>
  )
}

type State = { kind: 'loading' } | { kind: 'out'; note: string | null } | { kind: 'in'; account: Account }

/**
 * The account (spec §5.2): its address, "Your orders" (submitted orders only, each
 * linking to its order page through `ACCOUNT_ORDER_URL`) and sign-out. A sign-out the shop
 * could not finish (503 `sign_out_incomplete`) still signed this device out; the page says
 * what the shop said. Sign-out gives the browser a new visitor, so the cart starts empty.
 */
export function AccountPage() {
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [orders, setOrders] = useState<AccountOrder[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [leaving, setLeaving] = useState(false)
  const draftChanged = useDraftChanged()

  useEffect(() => {
    let live = true
    store.account().then(
      (account) => {
        if (!live) return
        setState(account ? { kind: 'in', account } : { kind: 'out', note: null })
        if (!account) return
        store.accountOrders().then(
          (answer) => live && setOrders(answer.orders),
          (caught: unknown) => {
            if (!live) return
            if (caught instanceof StoreError && caught.status === 401) setState({ kind: 'out', note: null })
            else setError(errorText(caught))
          },
        )
      },
      (caught: unknown) => live && setError(errorText(caught)),
    )
    return () => {
      live = false
    }
  }, [])

  const signOut = async () => {
    setLeaving(true)
    setError(null)
    try {
      await store.signOut()
      setState({ kind: 'out', note: 'You are signed out.' })
      draftChanged(null)
    } catch (caught) {
      if (caught instanceof StoreError && caught.slug === 'sign_out_incomplete') {
        setState({ kind: 'out', note: caught.message })
        draftChanged(null)
      } else {
        setError(errorText(caught))
      }
    } finally {
      setLeaving(false)
    }
  }

  if (state.kind === 'loading') {
    return error ? (
      <p className="sf-error" role="alert">
        {error}
      </p>
    ) : (
      <p className="sf-lead">Loading…</p>
    )
  }

  if (state.kind === 'out') {
    return (
      <section>
        <h1 className="sf-h1">Your account</h1>
        {state.note && (
          <p className="sf-lead" role="status">
            {state.note}
          </p>
        )}
        <p className="sf-lead">Sign in to see your orders.</p>
        <Link className="sf-button" to="/account/sign-in">
          Sign in
        </Link>
      </section>
    )
  }

  return (
    <section>
      <h1 className="sf-h1">Your account</h1>
      <p className="sf-lead">{state.account.email}</p>
      <h2 className="sf-h2" id="account-orders">
        Your orders
      </h2>
      {orders === null ? (
        !error && <p className="sf-lead">Loading…</p>
      ) : orders.length === 0 ? (
        <p className="sf-lead">You have no orders yet.</p>
      ) : (
        <ul className="sf-items" aria-labelledby="account-orders">
          {orders.map((order) => (
            <OrderRow key={order.id} order={order} />
          ))}
        </ul>
      )}
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      <p className="sf-actions">
        <button type="button" className="sf-button" onClick={signOut} disabled={leaving}>
          {leaving ? 'Signing out…' : 'Sign out'}
        </button>
      </p>
    </section>
  )
}
