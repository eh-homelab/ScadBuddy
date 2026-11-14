import { formatMoney } from '../../lib/money'
import type { StoreEstimate } from '../api/client'

/**
 * The rough price of a render (spec §5.4): always an estimate, never a quote. It says so in
 * the one line the visitor reads. A render with a part the shop could not measure (an
 * open mesh, #6155), or no filament at all, has no honest rough price: the page then says
 * the shop prices it, never the price book's floor as if it were the design's.
 */
export function Estimate({ estimate }: { estimate: StoreEstimate }) {
  const grams = Number(estimate.grams)
  if (estimate.unknown_parts > 0 || !(grams > 0)) {
    return (
      <div className="sf-estimate" role="status">
        <p className="sf-estimate-price">We price this design by hand</p>
        <p className="sf-estimate-note">Add it to your order, and we will send you the price before you pay.</p>
      </div>
    )
  }
  const price = formatMoney(estimate.price_minor, estimate.currency, estimate.minor_exponent)
  return (
    <div className="sf-estimate" role="status">
      <p className="sf-estimate-price">{`about ${price}, confirmed after we check it`}</p>
      <p className="sf-estimate-note">For one, from about {grams.toFixed(1)} g of filament.</p>
    </div>
  )
}
