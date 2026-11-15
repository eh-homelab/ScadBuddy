import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { formatMoney } from '../../lib/money'
import { orderPreview, type CustomerSource, type DraftSource, type OrderReader } from '../api/client'
import { EXPIRED } from '../models'

/**
 * A request's files and links in the cart (#6133, spec 2026-11-13 §4.3): a file reads
 * "Checking", then its preview and "Ready", or the check's plain reason. Each link and each
 * thing to buy is text only, never an anchor: the shop never opens a customer's link for
 * them, and neither does the page.
 */
export function CartSources({ sources }: { sources: readonly DraftSource[] }) {
  const models = sources.filter((s) => s.purpose === 'print')
  const buy = sources.filter((s) => s.purpose === 'purchase')
  return (
    <>
      {models.length > 0 && (
        <ul className="sf-sources" aria-label="Your models">
          {models.map((source) => (
            <li key={source.id} className="sf-source">
              {source.kind === 'file' ? <CartFile source={source} /> : <LinkText url={source.url} />}
            </li>
          ))}
        </ul>
      )}
      {buy.length > 0 && <BuyList items={buy} />}
    </>
  )
}

function CartFile({ source }: { source: DraftSource }) {
  const expired = source.check_reason === EXPIRED
  return (
    <>
      {source.check_status === 'ready' && source.preview_url && (
        <img className="sf-source-preview" src={source.preview_url} alt={`Preview of ${source.filename ?? 'the file'}`} />
      )}
      <div className="sf-source-body">
        <span className="sf-source-name">{source.filename}</span>
        {source.check_status === 'checking' && <span className="sf-state sf-state-wait">Checking</span>}
        {source.check_status === 'ready' && <span className="sf-state sf-state-ok">Ready</span>}
        {source.check_status === 'failed' && (
          <span className="sf-hint">We could not check this file. The shop will look at it.</span>
        )}
        {source.check_status === 'refused' &&
          (expired ? (
            <span className="sf-hint sf-hint-bad">
              This file expired; <Link to="/print-my-model">upload it again</Link>.
            </span>
          ) : (
            <span className="sf-hint sf-hint-bad">{source.check_reason ?? 'This file cannot be printed.'}</span>
          ))}
      </div>
    </>
  )
}

/** A customer's link, shown back as escaped text in a text node: never an anchor (spec §9.3). */
function LinkText({ url }: { url?: string | null }) {
  return (
    <div className="sf-source-body">
      <span className="sf-source-link">{url}</span>
    </div>
  )
}

function BuyList({
  items,
  amount,
}: {
  items: readonly { url?: string | null; note?: string; status?: string; amount_minor?: number | null; shop_note?: string }[]
  amount?: (minor: number) => string
}) {
  return (
    <div className="sf-buy-shown">
      <h3 className="sf-label">Things to buy</h3>
      <ul className="sf-sources">
        {items.map((item, index) => (
          <li key={index} className="sf-source">
            <div className="sf-source-body">
              <span className="sf-source-link">{item.url}</span>
              {item.note && <span className="sf-notes">{item.note}</span>}
              {item.status === 'pending' && amount && <span className="sf-state sf-state-wait">The shop is pricing it</span>}
              {item.status === 'recorded' && amount && item.amount_minor != null && (
                <span className="sf-state sf-state-ok">{`Bought for ${amount(item.amount_minor)}`}</span>
              )}
              {item.status === 'not_needed' && <span className="sf-hint">Not needed</span>}
              {item.shop_note && <span className="sf-notes">{item.shop_note}</span>}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * A request's files and links on the order page (#6133, spec 2026-11-13 §10.5). A file's
 * preview comes from the edge's route by a POST of the reader (`orderPreview`), so no URL
 * holds the order link's token. A file the order's end removed shows the shop's reason and
 * "Upload it again", a link to the form. Links are text only.
 */
export function OrderSources({
  sources,
  who,
  currency,
  minorExponent,
}: {
  sources: readonly CustomerSource[]
  who: OrderReader
  currency: string
  minorExponent: number
}) {
  const models = sources.filter((s) => s.purpose === 'print')
  const buy = sources.filter((s) => s.purpose === 'purchase')
  return (
    <>
      {models.length > 0 && (
        <ul className="sf-sources" aria-label="Your models">
          {models.map((source, index) => (
            <li key={index} className="sf-source">
              {source.kind === 'file' ? <OrderFile source={source} who={who} /> : <LinkText url={source.url} />}
            </li>
          ))}
        </ul>
      )}
      {buy.length > 0 && <BuyList items={buy} amount={(minor) => formatMoney(minor, currency, minorExponent)} />}
    </>
  )
}

function OrderFile({ source, who }: { source: CustomerSource; who: OrderReader }) {
  const [preview, setPreview] = useState<string | null>(null)
  const key = 'token' in who ? who.token : who.order_id
  useEffect(() => {
    if (!source.preview_url) return
    let live = true
    let made: string | null = null
    void orderPreview(who, source.preview_url).then((url) => {
      made = url
      if (live) setPreview(url)
      else if (url) URL.revokeObjectURL(url)
    })
    return () => {
      live = false
      if (made) URL.revokeObjectURL(made)
    }
    // The reader's identity is its token or its order id.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source.preview_url, key])
  return (
    <>
      {preview && <img className="sf-source-preview" src={preview} alt={`Preview of ${source.filename ?? 'the file'}`} />}
      <div className="sf-source-body">
        <span className="sf-source-name">{source.filename}</span>
        {source.status === 'imported' && <span className="sf-state sf-state-ok">Ready to price</span>}
        {source.status === 'declined' && source.customer_reason && (
          <span className="sf-hint sf-hint-bad">
            {source.customer_reason} <Link to="/print-my-model">Upload it again</Link>
          </span>
        )}
        {source.status === 'declined' && !source.customer_reason && <span className="sf-hint">The shop will not print this file.</span>}
        {source.shop_note && <span className="sf-notes">{source.shop_note}</span>}
      </div>
    </>
  )
}
