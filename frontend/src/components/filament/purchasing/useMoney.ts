import { useEffect, useState } from 'react'
import { api } from '../../../api/client'
import { formatCost, formatDecimal } from '../../../lib/decimal'

export interface ShopMoney {
  /** The shop's currency code, `null` until it is read. */
  currency: string | null
  /** The shop currency's minor-unit digits (2 until it is read). */
  exponent: number
  /**
   * A decimal string in `currency` (the shop's when none is given), with the digits it holds.
   * With no currency known (the price book is unread or failed) it is the bare decimal, with
   * no symbol: a guessed currency would show a wrong amount.
   */
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
    exponent: shop?.exponent ?? 2,
    format: (amount, currency) => {
      const code = currency ?? shop?.currency
      if (!code) return formatDecimal(amount)
      return formatCost(amount, code, shop && shop.currency === code ? shop.exponent : 2)
    },
  }
}
