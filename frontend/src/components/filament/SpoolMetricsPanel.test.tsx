import { screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SpoolHistory } from '../../api/filament'
import { renderPage } from '../../test/utils'
import { SpoolMetricsPanel } from './SpoolMetricsPanel'

const NOW = '2026-11-18T12:00:00Z'

const history = (values: Partial<SpoolHistory['metrics']> = {}, typeError: SpoolHistory['type_ams_error'] = null): SpoolHistory => ({
  events: [],
  series: [],
  metrics: {
    used_g: 120,
    consumed_g: 120,
    prints: 3,
    grams_per_print: 40,
    wasted_g: 30,
    cost_used: null,
    value_left: null,
    burn_g_per_day: 7,
    empty_on: '2027-03-15',
    opened_at: '2026-11-03T20:00:00Z',
    days_since_opened: 14.7,
    days_since_last_use: 9,
    hours_outside_dry: 0,
    ams_error: null,
    ...values,
  },
  type_ams_error: typeError,
  currency: 'USD',
  window_days: 30,
})

const tile = (name: string) => within(screen.getByLabelText('Spool metrics')).getByText(name).parentElement!

describe('the spool metrics panel', () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ['Date'] }).setSystemTime(NOW))
  afterEach(() => vi.useRealTimers())

  it("shows the type's AMS error beside the reason when the spool has no pair of its own", () => {
    renderPage(<SpoolMetricsPanel history={history({}, { pairs: 4, mean_abs_g: 12.5, bias_g: 3 })} gramsLeft={820} createdAt="2026-10-01T09:00:00Z" />)
    const ams = tile('AMS error')
    expect(ams).toHaveTextContent('—')
    expect(ams).toHaveTextContent('No AMS reading is paired with a scale reading yet')
    expect(ams).toHaveTextContent('Its type: ±12.5 g over 4 pairs')
  })

  it('says the burn is over the window for a spool older than it', () => {
    renderPage(<SpoolMetricsPanel history={history()} gramsLeft={820} createdAt="2026-10-01T09:00:00Z" />)
    expect(tile('Burn rate')).toHaveTextContent('Over the last 30 days')
  })

  it('says the burn is since it was opened for a spool younger than the window, as metrics.burn divides by its age', () => {
    renderPage(<SpoolMetricsPanel history={history()} gramsLeft={820} createdAt="2026-11-01T09:00:00Z" />)
    expect(tile('Burn rate')).toHaveTextContent('Since it was added 17 days ago')
    expect(tile('Burn rate')).not.toHaveTextContent('Over the last')
  })
})
