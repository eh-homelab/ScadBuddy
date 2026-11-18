import { useEffect, useState } from 'react'
import { api } from '../../../api/client'
import { formatCost } from '../../../lib/decimal'

export interface ShopMoney {
  /** The shop's currency code, `null` until it is read. */
  currency: string | null
  /** A decimal string in `currency` (the shop's when none is given), with the digits it holds. */
  format: (amount: string, currency?: string) => string
}

/** The shop's currency and its minor-unit digits (the price book's), for showing a purchasing amount. */
export function useMoney(): ShopMoney {
  const [shop, setShop] = useState<{ currency: string; exponent: number } | null>(null)
  useEffect(() => {
    let live = true
    api.priceBook().then(
      (book) => {
        if (live) setShop({ currency: book.currency, exponent: book.minor_exponent })
      },
      () => undefined,
    )
    return () => {
      live = false
    }
  }, [])
  return {
    currency: shop?.currency ?? null,
    format: (amount, currency) => {
      const code = currency ?? shop?.currency ?? 'USD'
      return formatCost(amount, code, shop && shop.currency === code ? shop.exponent : 2)
    },
  }
}
