import { CronExpressionParser } from 'cron-parser'

// When a cron job or a wakeup fires. Claude Code's CronCreate takes "standard 5-field
// cron in the user's local timezone" and its recurring jobs "auto-expire after 7 days";
// ScheduleWakeup's delay is "clamped to [60, 3600]" (the tools' descriptions, Claude
// Code 2.1.296). The agent has no user timezone, so it uses its own (`TZ`, UTC unless
// the deployment sets one), and every answer names it.

/** A recurring cron job's life, as Claude Code's. */
export const CRON_EXPIRY_MS = 7 * 24 * 60 * 60 * 1000
export const WAKEUP_MIN_S = 60
export const WAKEUP_MAX_S = 3600

/** The agent's timezone: the `TZ` its process runs with. */
export function agentTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

export class CronError extends Error {}

/** The next time `cron` fires strictly after `after`, or undefined if it never does. */
export function nextFire(cron: string, after: Date, tz: string): Date | undefined {
  const fields = cron.trim().split(/\s+/)
  if (fields.length !== 5) throw new CronError(`Invalid cron expression '${cron}'. Expected 5 fields: M H DoM Mon DoW.`)
  let expression
  try {
    expression = CronExpressionParser.parse(fields.join(' '), { currentDate: after, tz, strict: false })
  } catch (err) {
    throw new CronError(`Invalid cron expression '${cron}': ${(err as Error).message}`)
  }
  if (!expression.hasNext()) return undefined
  return expression.next().toDate()
}

const pad = (n: number) => String(n).padStart(2, '0')

function clock(hour: number, minute: number): string {
  const h = hour % 12 === 0 ? 12 : hour % 12
  return `${h}:${pad(minute)} ${hour < 12 ? 'AM' : 'PM'}`
}

/**
 * The words Claude Code shows for a cron expression, for the forms it was seen to name
 * ("Every 5 minutes", "Every hour at :07", "Weekdays at 9:00 AM"); any other shows as
 * the expression itself, as Claude Code shows "30 14 28 2 *".
 */
export function describeCron(cron: string): string {
  const f = cron.trim().split(/\s+/)
  if (f.length !== 5) return cron
  const [m, h, dom, mon, dow] = f as [string, string, string, string, string]
  const every = /^\*\/(\d+)$/.exec(m)
  if (every && h === '*' && dom === '*' && mon === '*' && dow === '*') {
    return every[1] === '1' ? 'Every minute' : `Every ${every[1]} minutes`
  }
  if (m === '*' && h === '*' && dom === '*' && mon === '*' && dow === '*') return 'Every minute'
  if (/^\d+$/.test(m) && h === '*' && dom === '*' && mon === '*' && dow === '*') return `Every hour at :${pad(Number(m))}`
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === '*' && mon === '*') {
    const at = clock(Number(h), Number(m))
    if (dow === '*') return `Every day at ${at}`
    if (dow === '1-5') return `Weekdays at ${at}`
  }
  return cron.trim()
}

/** A time of day as Claude Code's ScheduleWakeup answer shows it ("01:06:00"), in `tz`. */
export function timeOfDay(at: Date, tz: string): string {
  return new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(at)
}
