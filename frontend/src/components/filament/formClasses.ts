/** A table's head cell and body cell: dense, left-aligned; numbers add `text-right tabular-nums`. */
export const TH = 'px-2 py-1.5 font-medium'
export const TD = 'px-2 py-1.5'

/** A date and time as a person reads it; the ISO string stays in `<time dateTime>`. */
export const when = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
