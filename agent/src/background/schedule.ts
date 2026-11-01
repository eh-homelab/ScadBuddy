import { CronExpressionParser } from 'cron-parser'

// When a cron job or a wakeup fires. Claude Code's CronCreate takes "standard 5-field
// cron in the user's local timezone" and its recurring jobs "auto-expire after 7 days";
// ScheduleWakeup's delay is "clamped to [60, 3600]" (the tools' descriptions, Claude
// Code 2.1.296). The agent has no user timezone, so it uses its own (`TZ`, UTC unless
// the deployment sets one), and every answer names it. Claude Code itself always runs
// in UTC (harness/options.ts, #4107), so where a deployment sets another `TZ` the model's
// own "today" is UTC while these answers name the agent's zone.

/** A recurring cron job's life, as Claude Code's. */
export const CRON_EXPIRY_MS = 7 * 24 * 60 * 60 * 1000
export const WAKEUP_MIN_S = 60
export const WAKEUP_MAX_S = 3600

/** The agent's timezone: the `TZ` its process runs with. */
export function agentTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

export class CronError extends Error {}

// What a field may hold for a new job (`checkCron`, CronCreate): numbers, `*`, ranges, steps
// and lists, and names in the month and day-of-week fields. The rest of cron-parser's syntax
// (`L`, `W`, `#`, `?`) is refused, because a recurring job is a Temporal Schedule
// (background/temporal.ts), and Temporal refuses `L`, `W` and `?` and reads `#` its own way
// (measured against the Temporal CLI 1.5 dev server, 2026-11-01). A job stored before with
// that syntax keeps its timer workflow (`schedulable`), and nextFire still reads it.
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
const DAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
const FIELD_NAMES: readonly (readonly string[])[] = [[], [], [], MONTHS, DAYS]

function plainField(field: string, names: readonly string[]): boolean {
  return field
    .split(/[,/-]/)
    .every((part) => part === '*' || /^\d+$/.test(part) || names.includes(part.toLowerCase()))
}

function fieldsOf(cron: string): string[] {
  const fields = cron.trim().split(/\s+/)
  if (fields.length !== 5) throw new CronError(`Invalid cron expression '${cron}'. Expected 5 fields: M H DoM Mon DoW.`)
  return fields
}

/** Refuses, as a CronError, what a Schedule cannot express. */
export function checkCron(cron: string): string[] {
  const fields = fieldsOf(cron)
  const odd = fields.findIndex((f, i) => !plainField(f, FIELD_NAMES[i]!))
  if (odd >= 0) {
    throw new CronError(
      `Invalid cron expression '${cron}': field ${odd + 1} ('${fields[odd]}') may hold only numbers, *, ranges (a-b), steps (/n), lists (a,b) and month or day names.`,
    )
  }
  return fields
}

/** Whether a Schedule can express a cron: false for one stored before checkCron with `L`, `W`, `#` or `?`. */
export function schedulable(cron: string): boolean {
  try {
    checkCron(cron)
    return true
  } catch (err) {
    if (err instanceof CronError) return false
    throw err
  }
}

/**
 * The cron expressions of a job's Schedule. When neither day field is `*`, cron-parser
 * fires when either matches (as standard cron does) and Temporal only when both do
 * (measured as above; a step such as `*\/2` counts as restricted in both), so such a job
 * is two expressions, one per day field, whose union is cron-parser's.
 */
export function scheduleCrons(cron: string): string[] {
  const [m, h, dom, mon, dow] = checkCron(cron)
  if (dom === '*' || dow === '*') return [[m, h, dom, mon, dow].join(' ')]
  return [`${m} ${h} ${dom} ${mon} *`, `${m} ${h} * ${mon} ${dow}`]
}

/** The next time `cron` fires strictly after `after`, or undefined if it never does. */
export function nextFire(cron: string, after: Date, tz: string): Date | undefined {
  const fields = fieldsOf(cron)
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
