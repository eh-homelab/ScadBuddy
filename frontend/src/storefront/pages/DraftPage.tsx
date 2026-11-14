import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { ActionKey, errorText, store, type Draft, type DraftItem, type SubmittedOrder } from '../api/client'
import { SubmitForm } from '../components/SubmitForm'
import { useDraftChanged } from '../draftCount'
import { valueText } from '../params'

/**
 * `/draft`: the visitor's order before it is sent (spec §5.4). Each item can be removed,
 * one at a time: every Remove waits while one runs, so the answers come back in order and
 * the last one is the cart. A request's text, a file's name and each value are the
 * visitor's own, shown as text; each value is named by its label. Below the items, the
 * visitor sends the order (`SubmitForm`, spec §5.5); once sent, the page says where the
 * link to it went.
 */
export function DraftPage() {
  const [draft, setDraft] = useState<Draft | null>()
  const [error, setError] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState<SubmittedOrder | null>(null)
  const keys = useRef(new ActionKey())
  const draftChanged = useDraftChanged()

  useEffect(() => {
    let live = true
    store.draft().then(
      (answer) => {
        if (!live) return
        setDraft(answer)
        draftChanged(answer)
      },
      (caught: unknown) => live && setError(errorText(caught)),
    )
    return () => {
      live = false
    }
  }, [draftChanged])

  // The cart was sent from another tab: show what is there now, with the shop's answer.
  const sentElsewhere = (message: string) => {
    store.draft().then(
      (answer) => {
        setDraft(answer)
        draftChanged(answer)
        setError(message)
      },
      (caught: unknown) => setError(errorText(caught)),
    )
  }

  const remove = async (item: DraftItem) => {
    if (removing !== null) return
    setRemoving(item.id)
    setError(null)
    try {
      const next = await store.removeItem(item.id, keys.current.for(`remove:${item.id}`))
      keys.current.done()
      setDraft(next)
      draftChanged(next)
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setRemoving(null)
    }
  }

  const sent = (order: SubmittedOrder) => {
    setSubmitted(order)
    // The submitted order is no longer the visitor's cart.
    draftChanged(null)
  }

  if (submitted) {
    return (
      <section>
        <h1 className="sf-h1">Your order</h1>
        <div className="sf-panel" role="status">
          <p className="sf-lead">{`We have your order. We sent a link to ${submitted.email}.`}</p>
          <p className="sf-hint">{`Your order number is ${submitted.number}. Follow the link in the mail to see your order.`}</p>
        </div>
      </section>
    )
  }

  const items = draft?.items ?? []
  return (
    <section>
      <h1 className="sf-h1">Your order</h1>
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      {draft === undefined && !error && <p className="sf-hint">Loading your order…</p>}
      {draft !== undefined && items.length === 0 && (
        <div className="sf-panel">
          <p className="sf-lead">Your order is empty.</p>
          <Link className="sf-button" to="/">
            Browse the designs
          </Link>
        </div>
      )}
      {items.length > 0 && (
        <ul className="sf-items">
          {items.map((item) => (
            <li key={item.id} className="sf-item">
              <div className="sf-item-head">
                <h2 className="sf-item-title">{item.title}</h2>
                <span className="sf-item-qty">{`Quantity ${item.quantity}`}</span>
              </div>
              {item.inputs && Object.keys(item.inputs).length > 0 && (
                <dl className="sf-values">
                  {Object.entries(item.inputs).map(([name, value]) => (
                    <div key={name}>
                      <dt>{item.labels?.[name] ?? name}</dt>
                      <dd>{valueText(value)}</dd>
                    </div>
                  ))}
                </dl>
              )}
              {item.inputs && Object.keys(item.inputs).length === 0 && <p className="sf-hint">As the shop designed it.</p>}
              {item.notes !== null && <p className="sf-notes">{item.notes}</p>}
              {item.attachments.length > 0 && (
                <ul className="sf-filelist">
                  {item.attachments.map((file) => (
                    <li key={file.url}>
                      <a href={file.url} rel="noopener noreferrer" target="_blank">
                        {file.filename}
                      </a>
                    </li>
                  ))}
                </ul>
              )}
              <button
                type="button"
                className="sf-link"
                aria-label={`Remove ${item.title}`}
                disabled={removing !== null}
                onClick={() => void remove(item)}
              >
                {removing === item.id ? 'Removing…' : 'Remove'}
              </button>
            </li>
          ))}
        </ul>
      )}
      {draft && items.length > 0 && <SubmitForm draft={draft} onSubmitted={sent} onSent={sentElsewhere} />}
    </section>
  )
}
