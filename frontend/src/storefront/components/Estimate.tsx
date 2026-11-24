import { formatMoney } from '../../lib/money'
import type { StoreEstimate } from '../api/client'

/**
 * The price of a render (#6153, spec 2026-11-13 §5.4). A slice through the shop's own
 * quote path gives it, so it is firm: the page shows the price and nothing else, never
 * grams or print time (owner, 2026-11-14). While the slice runs, the page says it is
 * working the price out. With no firm price (the slice failed, or the shop cannot slice
 * this design), it shows no number at all: the shop prices the design by hand.
 *
 * #6921: the owner's rule is "always show a price". With `updating`, the price on screen
 * is not yet the firm price of the choices on screen (a `last` price, or the price of the
 * choices before a change): it stays visible, marked as updating, until the render's
 * estimate answers. A `last` price is never shown as firm, whatever `updating` says.
 *
 * #6710: with `held` (a file listing's page), no update will come: the price stays, and the
 * page says it is the last one, not the price of the choices on screen.
 */
export function Estimate({ estimate, updating = false, held = false }: { estimate: StoreEstimate; updating?: boolean; held?: boolean }) {
  if (estimate.source === 'pending') {
    return (
      <div className="sf-estimate" role="status">
        <p className="sf-estimate-price">Working out the price…</p>
      </div>
    )
  }
  const priced = (estimate.source === 'slice' || estimate.source === 'last') && estimate.price_minor != null && estimate.currency
  if (!priced) {
    return (
      <div className="sf-estimate" role="status">
        <p className="sf-estimate-price">We price this design by hand</p>
        <p className="sf-estimate-note">Add it to your order, and we will send you the price before you pay.</p>
      </div>
    )
  }
  const price = formatMoney(estimate.price_minor ?? 0, estimate.currency ?? '', estimate.minor_exponent ?? 2)
  if (held) {
    return (
      <div className="sf-estimate" role="status">
        <p className="sf-estimate-price">{`${price} for one`}</p>
        <p className="sf-estimate-note">This was our last price. We confirm the price before you pay.</p>
      </div>
    )
  }
  if (updating || estimate.source === 'last') {
    return (
      <div className="sf-estimate sf-estimate-updating" role="status" aria-busy="true">
        <p className="sf-estimate-price">{`${price} for one`}</p>
        <p className="sf-estimate-note">Updating this price…</p>
      </div>
    )
  }
  return (
    <div className="sf-estimate" role="status">
      <p className="sf-estimate-price">{`${price} for one`}</p>
    </div>
  )
}
