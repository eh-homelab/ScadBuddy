import type { PrinterNow } from '../../api/types'

/** One printer's card as shown: its last good read, and why that is not this read. */
export interface NowCard {
  printer: PrinterNow
  /** When `printer` was read, in ms since the epoch. */
  seenAt: number
  /** Why the card is not current (a failed or empty read), or `null` while it is. */
  stale: string | null
}

const UNLISTED = 'Bambuddy did not list this printer in the last read.'

/**
 * #3632 — the cards after a read. A printer whose status read failed keeps its last
 * good card, and one the read left out keeps its card, each marked stale: a "Finished,
 * clear the plate" card must not vanish for a poll because one Bambuddy read was slow.
 */
export function mergeCards(previous: NowCard[] | null, read: PrinterNow[], now: number): NowCard[] {
  const before = new Map((previous ?? []).map((card) => [card.printer.printer_id, card]))
  const cards = read.map((printer): NowCard => {
    const last = before.get(printer.printer_id)
    if (printer.error && last && !last.printer.error) return { ...last, stale: printer.error }
    return { printer, seenAt: now, stale: null }
  })
  const listed = new Set(read.map((printer) => printer.printer_id))
  for (const card of previous ?? []) {
    if (!listed.has(card.printer.printer_id)) cards.push({ ...card, stale: card.stale ?? UNLISTED })
  }
  return cards
}
