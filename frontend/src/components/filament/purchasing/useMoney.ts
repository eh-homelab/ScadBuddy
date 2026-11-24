import { useEffect, useState } from 'react'
import { api } from '../../../api/client'
import { formatCost, formatDecimal } from '../../../lib/decimal'

export interface ShopMoney {
  /** The shop's currency code, `null` until it is read. */
  currency: string | null
  /** The shop currency's minor-unit digits (2 until it is read: never convert a typed amount with it then). */
  exponent: number
  /** Why the price book could not be read, or null (R-P2-41: a cost input stays off without the currency). */
  error: string | null
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
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    api.priceBook().then(
      (book) => {
        if (live) setShop({ currency: book.currency, exponent: book.minor_exponent })
      },
      (cause: unknown) => {
        if (live) setError(cause instanceof Error ? cause.message : 'unknown error')
      },
    )
    return () => {
      live = false
    }
  }, [])
  return {
    currency: shop?.currency ?? null,
    exponent: shop?.exponent ?? 2,
    error,
    format: (amount, currency) => {
      const code = currency ?? shop?.currency
      if (!code) return formatDecimal(amount)
      return formatCost(amount, code, shop && shop.currency === code ? shop.exponent : 2)
    },
  }
}
