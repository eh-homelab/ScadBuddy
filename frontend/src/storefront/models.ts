import type { DraftSource, PurchaseLink } from './api/client'

/**
 * A customer's own models (#6133, spec 2026-11-13 §4, §6.1, §9.1): the caps and the link rule
 * the page applies before a byte is sent. The edge and the CRUD service check each again
 * (`backend/scadbuddy/storefront/wire.py`, `orders/customer_links.py`).
 */
export const MAX_MODEL_FILES = 20
export const MAX_MODEL_FILE_BYTES = 256 * 1024 * 1024
export const MAX_MODEL_REQUEST_BYTES = 1024 * 1024 * 1024
export const MAX_PRINT_LINKS = 10
export const MAX_PURCHASE_LINKS = 5
export const MAX_LINK_CHARS = 2048
export const MAX_LINK_NOTE = 500
export const MAX_COLOURS = 8
export const MAX_QUANTITY = 100
export const MAX_NOTE = 5000

/** The text a link that breaks the rule reads. */
export const LINK_PROBLEM = 'Give a link that starts with https:// or http://.'

/**
 * The link as the shop stores it, or null when it breaks the text rules (spec §9.1): at most
 * `MAX_LINK_CHARS`, no space or control character, `http` or `https`, a host, and no user
 * name or password. The page never fetches it and never shows it as an anchor.
 */
export function checkedLink(text: string): string | null {
  const link = text.trim()
  // eslint-disable-next-line no-control-regex
  if (!link || link.length > MAX_LINK_CHARS || /[\s\u0000-\u001f\u007f]/.test(link)) return null
  let url: URL
  try {
    url = new URL(link)
  } catch {
    return null
  }
  if ((url.protocol !== 'https:' && url.protocol !== 'http:') || !url.hostname) return null
  if (url.username || url.password) return null
  return link
}

/** Whether the file's name says it is an STL or a 3MF (the shop checks its bytes, spec §7). */
export function isModelFile(file: File): boolean {
  return /\.(stl|3mf)$/i.test(file.name)
}

/** What is wrong with these files before any upload, or null. */
export function modelFilesProblem(files: readonly File[]): string | null {
  if (files.length > MAX_MODEL_FILES) return `Add at most ${MAX_MODEL_FILES} files.`
  if (files.some((file) => !isModelFile(file))) return 'Add only .stl or .3mf files.'
  if (files.some((file) => file.size < 1)) return 'A file is empty.'
  if (files.some((file) => file.size > MAX_MODEL_FILE_BYTES)) return 'Each file must be 256 MB or smaller.'
  if (files.reduce((sum, file) => sum + file.size, 0) > MAX_MODEL_REQUEST_BYTES) return 'Send at most 1 GB of files at once.'
  return null
}

/** A file's size in the units a person reads. */
export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** What is wrong with the things to buy, or null: each needs a link that keeps the rule. */
export function purchaseLinksProblem(links: readonly PurchaseLink[]): string | null {
  if (links.length > MAX_PURCHASE_LINKS) return `Add at most ${MAX_PURCHASE_LINKS} things to buy.`
  return links.some((link) => checkedLink(link.url) === null) ? LINK_PROBLEM : null
}

/** The things to buy as the shop takes them: each link trimmed, each note trimmed. */
export function sentPurchaseLinks(links: readonly PurchaseLink[]): PurchaseLink[] {
  return links.map((link) => ({ url: link.url.trim(), note: link.note.trim() }))
}

/** The check reason of a staged file the cart kept too long (backend `model_requests.EXPIRED`). */
export const EXPIRED = 'This file expired before the order was sent; upload it again.'

/** Whether a cart line blocks the submit: its check refused it, or it expired (spec §4.3). */
export function blocksSubmit(source: DraftSource): boolean {
  return source.kind === 'file' && source.check_status === 'refused'
}
