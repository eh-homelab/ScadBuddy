import { readFileSync } from 'node:fs'
import { afterAll, afterEach, describe, expect, it } from 'vitest'
import { DEFAULT_DAYS, periodOf, shownPeriod } from './profit'

/** The server's default period length, from the exported spec (`ProfitReport.default_days`). */
function specDefaultDays(): number {
  const path = process.env.SCADBUDDY_OPENAPI_JSON ?? `${import.meta.dirname}/../../../../backend/openapi.json`
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (cause) {
    throw new Error(`no API spec at ${path}: run \`pnpm gen:api\` first (pnpm test does)`, { cause })
  }
  const spec = JSON.parse(text) as { components?: { schemas?: { ProfitReport?: { properties?: { default_days?: { default?: unknown } } } } } }
  const days = spec.components?.schemas?.ProfitReport?.properties?.default_days?.default
  if (typeof days !== 'number') throw new Error(`ProfitReport.default_days has no default in the spec at ${path}`)
  return days
}

// Node reads `TZ` again each time it changes, so each case runs in the zone it names.
const zone = process.env.TZ
afterEach(() => {
  process.env.TZ = zone
})
afterAll(() => {
  process.env.TZ = zone
})

describe('the profit report period (#5546)', () => {
  it('has the server’s default length', () => {
    expect(DEFAULT_DAYS).toBe(specDefaultDays())
  })

  it.each([
    ['UTC', '2026-10-01T00:00:00.000Z', '2026-11-01T00:00:00.000Z'],
    // Los Angeles is UTC-7 on Oct 1 and UTC-7 until Nov 1, 09:00 UTC (DST ends Nov 1).
    ['America/Los_Angeles', '2026-10-01T07:00:00.000Z', '2026-11-01T07:00:00.000Z'],
    // Auckland is UTC+13 in October (DST from Sep 27).
    ['Pacific/Auckland', '2026-09-30T11:00:00.000Z', '2026-10-31T11:00:00.000Z'],
  ])('covers the days picked from local midnight in %s', (tz, from, to) => {
    process.env.TZ = tz
    expect(periodOf('2026-10-01', '2026-10-31')).toEqual({ from, to })
  })

  it('crosses a DST change by the calendar, not by 24 hours', () => {
    process.env.TZ = 'America/Los_Angeles'
    // Nov 1 is 25 hours long there: its end is 08:00 UTC on Nov 2.
    expect(periodOf('2026-11-01', '2026-11-01')).toEqual({ from: '2026-11-01T07:00:00.000Z', to: '2026-11-02T08:00:00.000Z' })
  })

  it('shows the default period at once: DEFAULT_DAYS days to `to`, or to today', () => {
    process.env.TZ = 'America/Los_Angeles'
    // 23:30 on Nov 5 in Los Angeles is Nov 6 in UTC: today is still Nov 5.
    const now = new Date('2026-11-06T07:30:00Z')
    expect(shownPeriod('', '', now)).toEqual({ from: '2026-08-08', to: '2026-11-05' })
    expect(shownPeriod('', '2026-03-31', now)).toEqual({ from: '2026-01-01', to: '2026-03-31' })
    expect(shownPeriod('2026-10-01', '', now)).toEqual({ from: '2026-10-01', to: '2026-11-05' })
    // A `to` long past with no `from`: the request sends the `from` the inputs show.
    expect(periodOf('', '2026-03-31', now)).toEqual({ from: '2026-01-01T08:00:00.000Z', to: '2026-04-01T07:00:00.000Z' })
    // A URL value that is no day is no value.
    expect(shownPeriod('soon', 'x', now)).toEqual(shownPeriod('', '', now))
  })
})
