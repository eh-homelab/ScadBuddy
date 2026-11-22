import { Link } from 'react-router'
import type { SpoolInvoiceRef, SpoolTypeSupplier } from '../../../api/filamentPurchasing'
import { filamentLinks } from '../../../lib/filamentLinks'
import { SafeLink } from './SafeLink'
import { useMoney } from './useMoney'

/**
 * #6266 — on a spool's page, under its cost: the invoice it was bought on (a link to the
 * invoice, with its line and unit cost) and where its type can be bought, each supplier's
 * SKU and newest price per kg. Nothing when the spool has neither.
 */
export function SpoolPurchasing({ invoice, suppliers }: { invoice: SpoolInvoiceRef | null | undefined; suppliers: SpoolTypeSupplier[] | undefined }) {
  const money = useMoney()
  const sellers = suppliers ?? []
  if (!invoice && sellers.length === 0) return null
  return (
    <dl className="flex flex-col gap-2 text-[13px]">
      {invoice && (
        <div className="flex flex-wrap gap-x-3">
          <dt className="w-24 shrink-0 text-muted-foreground">Invoice</dt>
          <dd>
            <Link to={filamentLinks.invoiceLine(invoice.invoice_id, invoice.line_id)} className="font-medium underline-offset-4 hover:underline">
              Invoice {invoice.number ?? '(no number)'}
            </Link>
            {' from '}
            <Link to={filamentLinks.supplier(invoice.supplier_id)} className="underline-offset-4 hover:underline">
              {invoice.supplier_name}
            </Link>
            <span className="text-muted-foreground">
              {' '}
              (line {invoice.line_position}, {money.format(invoice.unit_cost)} each)
            </span>
          </dd>
        </div>
      )}
      {sellers.length > 0 && (
        <div className="flex flex-wrap gap-x-3">
          <dt className="w-24 shrink-0 text-muted-foreground">Purchasing</dt>
          <dd>
            <ul className="flex flex-col gap-0.5">
              {sellers.map((seller) => (
                <li key={`${seller.supplier_id}-${seller.sku}`}>
                  <Link to={filamentLinks.supplier(seller.supplier_id)} className="underline-offset-4 hover:underline">
                    {seller.supplier_name}
                  </Link>{' '}
                  <span className="text-muted-foreground">
                    {seller.sku}
                    {seller.price_per_kg && `, ${money.format(seller.price_per_kg)} per kg`}
                  </span>
                  {seller.product_url && (
                    <>
                      {' '}
                      <SafeLink url={seller.product_url} />
                    </>
                  )}
                </li>
              ))}
            </ul>
          </dd>
        </div>
      )}
    </dl>
  )
}
