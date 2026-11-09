import { formatMoney } from '../../lib/money'
import type { StoreEstimate } from '../api/client'

/**
 * The rough price of a render (spec §5.4): always an estimate, never a quote. It says so in
 * the one line the visitor reads.
 */
export function Estimate({ estimate }: { estimate: StoreEstimate }) {
  const price = formatMoney(estimate.price_minor, estimate.currency, estimate.minor_exponent)
  return (
    <div className="sf-estimate" role="status">
      <p className="sf-estimate-price">{`about ${price}, confirmed after we check it`}</p>
      <p className="sf-estimate-note">For one, from about {Number(estimate.grams).toFixed(1)} g of filament.</p>
    </div>
  )
}
