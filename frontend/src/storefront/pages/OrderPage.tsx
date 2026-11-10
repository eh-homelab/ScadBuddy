import { Suspense, lazy, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router'
import { formatMoney } from '../../lib/money'
import { markPayFromOrder } from '../payReturn'
import {
  ActionKey,
  StoreError,
  errorText,
  store,
  type CustomerItem,
  type CustomerOrder,
  type CustomerPayment,
  type CustomerRevision,
  type OrderReader,
} from '../api/client'
import type { PreviewJob } from '../../components/Preview'

// three.js is most of the viewer: its own chunk, fetched only for a design with a 3D preview.
const Preview = lazy(async () => ({ default: (await import('../../components/Preview')).Preview }))

/** A design's 3D preview (`kind: 'model'`, a presigned `model/gltf-binary`) as the viewer's finished job. */
function modelJob(url: string): PreviewJob {
  return { id: url, status: 'done', preview_url: url, plates: null, bbox_mm: null, colors: [], error: null }
}

/** What a link that opens no order reads (an unknown, revoked or broken link). */
const NO_ORDER = 'This link does not open an order. It may have been replaced by a newer one: use the link in the latest mail from the shop.'

/** What `/o` reads with no token and no order id (the payment provider sends a payer here). */
const NO_LINK = 'Open your order from the link in your mail.'

/**
 * `/o#<token>`: the customer's order (spec §5.2, §5.6). The token is the order link's,
 * read from the URL fragment, which the browser never sends: the page posts it in each
 * request's body and never puts it into a path or the history. With no token,
 * `/o?order=<id>` (a signed-in account's "Your orders") posts the order's id instead, and
 * the shop reads it only for the session's own account. A design's 3D preview shows in
 * the shared viewer (`components/Preview.tsx`). Every text the customer or
 * the shop typed is a text node. Each design shows its pictures (a picture the shop cannot
 * show now is its caption) and its colours, which are part of what the customer accepts. A payment shows "Paid" only from the shop's own answer,
 * never because the provider sent the browser back here (spec §7): "Check for updates"
 * reads the order again.
 */
export function OrderPage() {
  const location = useLocation()
  const token = location.hash.replace(/^#/, '')
  const orderId = new URLSearchParams(location.search).get('order') ?? ''
  const who = useMemo<OrderReader | null>(
    () => (token ? { token } : orderId ? { order_id: orderId } : null),
    [token, orderId],
  )
  /** The writes' reader: they are offered only once `who` has read an order. */
  const reader: OrderReader = who ?? { token: '' }
  const [order, setOrder] = useState<CustomerOrder | null>()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const keys = useRef(new ActionKey())

  const load = useCallback(async () => {
    setError(null)
    try {
      if (who === null) return
      setOrder(await store.orderView(who))
    } catch (caught) {
      if (caught instanceof StoreError && caught.status === 404) setOrder(null)
      else setError(errorText(caught))
    }
  }, [who])

  useEffect(() => {
    if (who) void load()
    else setOrder(null)
  }, [who, load])

  /** One write at a time, each with its own key; the answer is the order after it. */
  const write = async (what: string, send: (key: string) => Promise<CustomerOrder>): Promise<boolean> => {
    if (busy) return false
    setBusy(true)
    setError(null)
    try {
      setOrder(await send(keys.current.for(what)))
      keys.current.done()
      return true
    } catch (caught) {
      if (caught instanceof StoreError && caught.status === 404 && caught.type === null) setOrder(null)
      else setError(errorText(caught))
      return false
    } finally {
      setBusy(false)
    }
  }

  if (who === null) {
    return (
      <section>
        <h1 className="sf-h1">Your order</h1>
        <div className="sf-panel">
          <p className="sf-lead">{NO_LINK}</p>
          <p className="sf-hint">
            Have an account? See <Link to="/account">Your orders</Link>.
          </p>
        </div>
      </section>
    )
  }

  if (order === null) {
    return (
      <section>
        <h1 className="sf-h1">Your order</h1>
        <div className="sf-panel">
          <p className="sf-lead">{NO_ORDER}</p>
        </div>
      </section>
    )
  }

  return (
    <section>
      <h1 className="sf-h1">{order ? `Order ${order.number}` : 'Your order'}</h1>
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      {order === undefined && !error && <p className="sf-hint">Loading your order…</p>}
      {order && (
        <>
          <p className="sf-lead" role="status">
            {order.status}
          </p>
          <ul className="sf-items">
            {order.items.map((item) => (
              <Item
                key={item.id}
                item={item}
                order={order}
                busy={busy}
                answer={(revision, answer, note) =>
                  write(`answer:${revision.id}:${answer}:${note}`, (key) =>
                    store.answerRevision(reader, revision.id, answer, note, key),
                  )
                }
                acceptPrice={(quoteId) => write(`accept:${quoteId}`, (key) => store.acceptQuote(reader, quoteId, key))}
              />
            ))}
          </ul>
          {order.payments.length > 0 && <Payments order={order} />}
          {order.shipments.length > 0 && (
            <div className="sf-panel">
              <h2 className="sf-item-title">Shipping</h2>
              <ul className="sf-filelist">
                {order.shipments.map((shipment, index) => (
                  <li key={index}>
                    {[shipment.carrier, shipment.service].filter(Boolean).join(' ') || 'Sent'}
                    {shipment.tracking && <span className="sf-notes">{`Tracking: ${shipment.tracking}`}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p>
            <button type="button" className="sf-button sf-button-quiet" disabled={busy} onClick={() => void load()}>
              Check for updates
            </button>
          </p>
          {order.can_cancel && (
            <Cancel busy={busy} cancel={(reason) => write(`cancel:${reason}`, (key) => store.cancelOrder(reader, reason, key))} />
          )}
        </>
      )}
    </section>
  )
}

type Answer = 'accepted' | 'changes_requested'

function Item({
  item,
  order,
  busy,
  answer,
  acceptPrice,
}: {
  item: CustomerItem
  order: CustomerOrder
  busy: boolean
  answer: (revision: CustomerRevision, answer: Answer, note: string) => Promise<boolean>
  acceptPrice: (quoteId: string) => Promise<boolean>
}) {
  const quote = item.quote
  return (
    <li className="sf-item">
      <div className="sf-item-head">
        <h2 className="sf-item-title">{item.title}</h2>
        <span className="sf-item-qty">{`Quantity ${item.quantity}`}</span>
      </div>
      <p className="sf-hint">{item.status}</p>
      {item.revisions.map((revision) => (
        <Revision key={revision.id} revision={revision} busy={busy} answer={answer} />
      ))}
      {quote && (
        <div className="sf-estimate">
          <p className="sf-estimate-price">{`Price: ${formatMoney(quote.total_minor, order.currency, order.minor_exponent)}`}</p>
          {quote.quantity > 1 && (
            <p className="sf-estimate-note">{`${formatMoney(quote.unit_price_minor, order.currency, order.minor_exponent)} each`}</p>
          )}
          {quote.accepted && <p className="sf-estimate-note">You accepted this price.</p>}
          {quote.can_accept && (
            <button type="button" className="sf-button" disabled={busy} onClick={() => void acceptPrice(quote.id)}>
              Accept price
            </button>
          )}
        </div>
      )}
    </li>
  )
}

function Revision({
  revision,
  busy,
  answer,
}: {
  revision: CustomerRevision
  busy: boolean
  answer: (revision: CustomerRevision, answer: Answer, note: string) => Promise<boolean>
}) {
  const [asking, setAsking] = useState(false)
  const [note, setNote] = useState('')
  const id = useId()
  return (
    <div className="sf-panel">
      <h3 className="sf-item-title">{`Design ${revision.number}`}</h3>
      {revision.note && <p className="sf-notes">{revision.note}</p>}
      {revision.pictures.map((picture, index) =>
        picture.url === null ? (
          <p key={index} className="sf-hint">{`${picture.caption}: this picture cannot be shown now.`}</p>
        ) : picture.kind === 'model' ? (
          <figure key={index} className="sf-stage" aria-label={picture.caption}>
            <Suspense fallback={<div className="sf-stage-empty">Loading the viewer…</div>}>
              <Preview job={modelJob(picture.url)} unit="mm" rendering={false} />
            </Suspense>
          </figure>
        ) : picture.kind === 'video' ? (
          <video key={index} className="sf-file" src={picture.url} controls aria-label={picture.caption} />
        ) : (
          <img key={index} className="sf-file" src={picture.url} alt={picture.caption} />
        ),
      )}
      {revision.colours.length > 0 && (
        <ul className="sf-filelist">
          {revision.colours.map((colour) => (
            <li key={colour.slot}>
              <span className="sf-swatch" style={{ background: colour.colour }} aria-hidden="true" />
              {`Colour ${colour.slot + 1}: ${colour.name ?? colour.colour}`}
            </li>
          ))}
        </ul>
      )}
      {revision.answer === 'accepted' && <p className="sf-hint">You accepted this design.</p>}
      {revision.answer === 'changes_requested' && (
        <>
          <p className="sf-hint">You asked for changes:</p>
          <p className="sf-notes">{revision.answer_note}</p>
        </>
      )}
      {revision.answer === 'superseded' && <p className="sf-hint">A newer design replaced this one.</p>}
      {revision.can_answer && !asking && (
        <div className="sf-order">
          <button type="button" className="sf-button" disabled={busy} onClick={() => void answer(revision, 'accepted', '')}>
            Accept
          </button>
          <button type="button" className="sf-button sf-button-quiet" disabled={busy} onClick={() => setAsking(true)}>
            Ask for changes
          </button>
        </div>
      )}
      {revision.can_answer && asking && (
        <form
          className="sf-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (!note.trim()) return
            void answer(revision, 'changes_requested', note).then((sent) => sent && setAsking(false))
          }}
        >
          <div className="sf-field">
            <label className="sf-label" htmlFor={`${id}-note`}>
              What should change?
            </label>
            <textarea
              id={`${id}-note`}
              className="sf-input sf-textarea"
              maxLength={4000}
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </div>
          <div className="sf-order">
            <button type="submit" className="sf-button" disabled={busy || !note.trim()}>
              Send
            </button>
            <button type="button" className="sf-button sf-button-quiet" onClick={() => setAsking(false)}>
              Back
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

function Payments({ order }: { order: CustomerOrder }) {
  const what = (payment: CustomerPayment) => (payment.kind === 'deposit' ? 'Deposit' : 'Payment')
  return (
    <div className="sf-panel">
      <h2 className="sf-item-title">Payments</h2>
      <ul className="sf-filelist">
        {order.payments.map((payment) => {
          const amount = formatMoney(payment.amount_minor, payment.currency, order.minor_exponent)
          return (
            <li key={payment.id}>
              {`${what(payment)} of ${amount}: `}
              {payment.status === 'paid' && <strong>Paid</strong>}
              {payment.status === 'pending' && <span>We are confirming your payment.</span>}
              {payment.status === 'open' &&
                (payment.pay_url ? (
                  <a className="sf-button" href={payment.pay_url}
                    rel="noreferrer"
                    onClick={(event) => markPayFromOrder(payment.pay_url!, event.nativeEvent)}
                  >
                    {payment.kind === 'deposit' ? `Pay deposit ${amount}` : `Pay ${amount}`}
                  </a>
                ) : (
                  <span>The shop will tell you how to pay.</span>
                ))}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function Cancel({ busy, cancel }: { busy: boolean; cancel: (reason: string) => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const id = useId()
  if (!open) {
    return (
      <p>
        <button type="button" className="sf-button sf-button-quiet" disabled={busy} onClick={() => setOpen(true)}>
          Cancel order
        </button>
      </p>
    )
  }
  return (
    <form
      className="sf-form sf-panel"
      onSubmit={(event) => {
        event.preventDefault()
        void cancel(reason.trim())
      }}
    >
      <div className="sf-field">
        <label className="sf-label" htmlFor={`${id}-reason`}>
          Why are you cancelling? (optional)
        </label>
        <textarea
          id={`${id}-reason`}
          className="sf-input sf-textarea"
          maxLength={1000}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </div>
      <div className="sf-order">
        <button type="submit" className="sf-button" disabled={busy}>
          Cancel my order
        </button>
        <button type="button" className="sf-button sf-button-quiet" onClick={() => setOpen(false)}>
          Keep my order
        </button>
      </div>
    </form>
  )
}
