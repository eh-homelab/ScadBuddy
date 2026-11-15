import { formatMoney } from '../../lib/money'
import type { StoreEstimate } from '../api/client'

/**
 * The price of a render (#6153, spec 2026-11-13 §5.4). A slice through the shop's own
 * quote path gives it, so it is firm: the page shows the price and nothing else, never
 * grams or print time (owner, 2026-11-14). While the slice runs, the page says it is
 * working the price out. With no firm price (the slice failed, or the shop cannot slice
 * this design), it shows no number at all: the shop prices the design by hand.
 */
export function Estimate({ estimate }: { estimate: StoreEstimate }) {
  if (estimate.source === 'pending') {
    return (
      <div className="sf-estimate" role="status">
        <p className="sf-estimate-price">Working out the price…</p>
      </div>
    )
  }
  if (estimate.source !== 'slice' || estimate.price_minor == null || !estimate.currency) {
    return (
      <div className="sf-estimate" role="status">
        <p className="sf-estimate-price">We price this design by hand</p>
        <p className="sf-estimate-note">Add it to your order, and we will send you the price before you pay.</p>
      </div>
    )
  }
  const price = formatMoney(estimate.price_minor, estimate.currency, estimate.minor_exponent ?? 2)
  return (
    <div className="sf-estimate" role="status">
      <p className="sf-estimate-price">{`${price} for one`}</p>
    </div>
  )
}
