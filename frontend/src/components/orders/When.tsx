import { formatFullTime, formatLocalTime } from '../../lib/format'

/** #5554 — a moment in the viewer's local time, with the full timestamp on hover. */
export function When({ at }: { at: number | string }) {
  const ms = typeof at === 'string' ? Date.parse(at) : at
  if (Number.isNaN(ms)) return null
  return (
    <time dateTime={new Date(ms).toISOString()} title={formatFullTime(ms)}>
      {formatLocalTime(ms)}
    </time>
  )
}
