import { StoreError, errorText, type Listing, type StoreEstimate } from './api/client'
import { clashText } from './colours'

/**
 * What the Customize page tells a visitor when the shop refuses or delays a read, shared by
 * a template listing's page and a file listing's (#6710).
 */

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** A poll that failed for a reason that passes: the network, a limit, a busy shop. */
export function passing(error: unknown): error is StoreError {
  return error instanceof StoreError && (error.status === 0 || error.status === 429 || error.status >= 500)
}

/** A wait as the visitor reads it: seconds to 90, then minutes to 90, then hours, rounded up. */
export function inTime(seconds: number | null): string {
  if (seconds === null) return 'in a minute'
  if (seconds > 90 * 60) return `in ${Math.ceil(seconds / 3600)} hours`
  if (seconds > 90) return `in ${Math.ceil(seconds / 60)} minutes`
  return seconds === 1 ? 'in 1 second' : `in ${seconds} seconds`
}

/**
 * What a visitor reads when a price is over a limit (#7523). The shop counts previews and
 * prices together. `next` says what comes after the wait: the page reads the price again on
 * its own, the visitor may, or (null) the wait is over.
 */
export function priceLimitText(error: StoreError, next: 'page' | 'visitor' | null): string {
  const why = error.slug === 'render_limited' ? 'You have asked for a lot of previews and prices.' : 'Your connection has sent a lot of requests.'
  if (next === null) return why
  return `${why} ${next === 'page' ? 'We will check this price again' : 'Try again'} ${inTime(error.retryAfter)}.`
}

/** What a visitor reads when a render, a price or an add of `listing` is refused. */
export function refusalText(error: unknown, listing: Listing | undefined): string {
  if (!(error instanceof StoreError)) return errorText(error)
  if (error.slug === 'render_limited') return `You have made a lot of previews. Try again ${inTime(error.retryAfter)}.`
  if (error.slug === 'render_queue_full') return `The shop is busy making other previews. Try again ${inTime(error.retryAfter)}.`
  if (error.slug === 'rate_limited') return `Your connection has sent a lot of requests. Try again ${inTime(error.retryAfter)}.`
  // #6258: the colours given share no allowed material: the text names the parts.
  if (error.slug === 'listing_colours_clash') return clashText(listing, error.parts)
  return errorText(error)
}

export function changedListing(error: unknown): boolean {
  return error instanceof StoreError && error.slug === 'listing_changed'
}

/** Whether a price has a number to show: a firm one, or the last one (#6921). */
export function hasNumber(estimate: StoreEstimate): boolean {
  return (estimate.source === 'slice' || estimate.source === 'last') && estimate.price_minor != null
}
