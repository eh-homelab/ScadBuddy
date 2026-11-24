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

function inSeconds(error: StoreError): string {
  if (error.retryAfter === null) return 'in a minute'
  return error.retryAfter === 1 ? 'in 1 second' : `in ${error.retryAfter} seconds`
}

/** What a visitor reads when a render, a price or an add of `listing` is refused. */
export function refusalText(error: unknown, listing: Listing | undefined): string {
  if (!(error instanceof StoreError)) return errorText(error)
  if (error.slug === 'render_limited') return `You have made a lot of previews. Try again ${inSeconds(error)}.`
  if (error.slug === 'render_queue_full') return `The shop is busy making other previews. Try again ${inSeconds(error)}.`
  if (error.slug === 'rate_limited') return `Your connection has sent a lot of requests. Try again ${inSeconds(error)}.`
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
