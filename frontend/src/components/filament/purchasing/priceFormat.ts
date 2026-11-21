import { ApiError } from '../../../api/client'

/** What a refused or failed request says: the server's own words, else `fallback`. */
export const message = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/** Today as the date input reads it: the viewer's local day, `YYYY-MM-DD`. */
export function today(now: Date = new Date()): string {
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

/** `change_pct` (one decimal, `"10.0"` or `"-5.2"`) as the chip says it: `+10.0 %`, `-5.2 %`. */
export function changeLabel(pct: string): string {
  return `${pct.startsWith('-') ? '' : '+'}${pct} %`
}
