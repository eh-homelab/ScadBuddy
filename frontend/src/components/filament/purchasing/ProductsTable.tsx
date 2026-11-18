import { Fragment, useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../../api/client'
import type { SpoolType } from '../../../api/filament'
import { patchProduct, type SupplierProduct } from '../../../api/filamentPurchasing'
import { Button } from '@/components/ui/button'
import { TD, TH } from '../formClasses'
import { RgbaSwatch } from '../RgbaSwatch'
import { AddPriceForm } from './AddPriceForm'
import { PriceCell } from './PriceCell'
import { SafeLink } from './SafeLink'
import type { ShopMoney } from './useMoney'

/**
 * #6266 — a supplier's products: SKU, the type each one is, its pack size, its page, and its
 * newest price. "Add price" opens a row under the product with the shared `AddPriceForm`.
 * `onChanged` runs after a price is added or a product archived, so the page reads again.
 */
export function ProductsTable({
  products,
  types,
  money,
  onChanged,
}: {
  products: SupplierProduct[]
  types: Map<string, SpoolType>
  money: ShopMoney
  onChanged: () => void
}) {
  const [adding, setAdding] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function archive(product: SupplierProduct) {
    setError(null)
    try {
      await patchProduct(product.id, { archived: !product.archived_at })
      onChanged()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The product could not be changed. Try again.')
    }
  }

  if (products.length === 0) return <p className="text-[13px] text-muted-foreground">No products yet. Add the first one to start its price history.</p>

  return (
    <div className="flex flex-col gap-2">
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      <div className="relative overflow-x-auto rounded-md border">
        <table aria-label="Products" className="w-full text-left text-[13px]">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th scope="col" className={TH}>SKU</th>
              <th scope="col" className={TH}>Type</th>
              <th scope="col" className={`${TH} text-right`}>Pack</th>
              <th scope="col" className={TH}>Product page</th>
              <th scope="col" className={`${TH} text-right`}>Newest price</th>
              <th scope="col" className={TH}>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {products.map((product) => {
              const type = types.get(product.type_id)
              const open = adding === product.id
              return (
                <Fragment key={product.id}>
                  <tr data-product={product.id} className={`border-t align-top ${product.archived_at ? 'text-muted-foreground' : ''}`}>
                    <td className={`${TD} font-mono text-[12px]`}>{product.sku}</td>
                    <td className={TD}>
                      <Link to={`/filament/types/${product.type_id}`} className="inline-flex items-center gap-2 underline-offset-4 hover:underline">
                        {type && <RgbaSwatch rgba={type.rgba ?? '#FFFFFFFF'} size="sm" />}
                        <span>{type?.code ?? 'Unknown type'}</span>
                      </Link>
                      {product.archived_at && <span className="ml-2 text-[12px]">Archived</span>}
                    </td>
                    <td className={`${TD} text-right tabular-nums`}>{product.pack_size}</td>
                    <td className={TD}>
                      <SafeLink url={product.product_url} />
                    </td>
                    <td className={`${TD} text-right`}>
                      <PriceCell point={product.newest_price} money={money} />
                    </td>
                    <td className={`${TD} whitespace-nowrap text-right`}>
                      {!open && !product.archived_at && (
                        <Button type="button" variant="ghost" size="sm" aria-label={`Add price for ${product.sku}`} onClick={() => setAdding(product.id)}>
                          Add price
                        </Button>
                      )}
                      <Button type="button" variant="ghost" size="sm" aria-label={`${product.archived_at ? 'Restore' : 'Archive'} ${product.sku}`} onClick={() => void archive(product)}>
                        {product.archived_at ? 'Restore' : 'Archive'}
                      </Button>
                    </td>
                  </tr>
                  {open && (
                    <tr className="bg-muted/30">
                      <td colSpan={6} className={TD}>
                        <div className="flex flex-wrap items-end gap-2">
                          <AddPriceForm
                            productId={product.id}
                            currency={money.currency}
                            label={product.sku}
                            onAdded={() => {
                              setAdding(null)
                              onChanged()
                              // The form is gone: focus goes back to the button that opened it.
                              requestAnimationFrame(() => [...document.querySelectorAll('button')].find((one) => one.getAttribute('aria-label') === `Add price for ${product.sku}`)?.focus())
                            }}
                          />
                          <Button type="button" variant="ghost" size="sm" onClick={() => setAdding(null)}>
                            Cancel
                          </Button>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
