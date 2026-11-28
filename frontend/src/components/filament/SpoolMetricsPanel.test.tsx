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
    non_print_waste_g: 0,
    cost_used: null,
    value_left: null,
    burn_g_per_day: 7,
    empty_on: '2027-03-15',
    opened_at: '2026-11-03T20:00:00Z',
    days_since_opened: 14.7,
    days_since_last_use: 9,
    hours_outside_dry: 0,
    ams_error: null,
    used_m: null,
    implied_density_g_cm3: null,
    density_g_cm3: null,
    density_mismatch: false,
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

  it('shows the metres and the implied density beside the grams (#7685)', () => {
    renderPage(<SpoolMetricsPanel history={history({ used_m: 3, implied_density_g_cm3: 1.2394, density_g_cm3: 1.24, density_mismatch: false })} gramsLeft={820} createdAt="2026-10-01T09:00:00Z" />)
    expect(tile('Used')).toHaveTextContent('3 prints, 40 g each, 3 m')
    const density = tile('Density')
    expect(density).toHaveTextContent('1.24 g/cm³')
    expect(density).toHaveTextContent('The type says 1.24 g/cm³')
    expect(density).not.toHaveTextContent('check the type')
  })

  it('says when the prints imply another density than the type (#7685)', () => {
    renderPage(<SpoolMetricsPanel history={history({ used_m: 3, implied_density_g_cm3: 1.31, density_g_cm3: 1.24, density_mismatch: true })} gramsLeft={820} createdAt="2026-10-01T09:00:00Z" />)
    expect(tile('Density')).toHaveTextContent("The prints imply 1.31 g/cm³, 6% over the type's 1.24 g/cm³: check the type's density or the label.")
    renderPage(<SpoolMetricsPanel history={history({ used_m: 3, implied_density_g_cm3: 1.1, density_g_cm3: 1.24, density_mismatch: true })} gramsLeft={820} createdAt="2026-10-01T09:00:00Z" />)
    expect(screen.getAllByText('Density')[1]!.parentElement).toHaveTextContent("The prints imply 1.1 g/cm³, 11% under the type's 1.24 g/cm³")
  })

  it('shows no metres or density for a spool with none (#7685)', () => {
    renderPage(<SpoolMetricsPanel history={history({ used_m: null, implied_density_g_cm3: null, density_g_cm3: null, density_mismatch: false })} gramsLeft={820} createdAt="2026-10-01T09:00:00Z" />)
    expect(tile('Used')).toHaveTextContent('3 prints, 40 g each')
    expect(tile('Used')).not.toHaveTextContent(' m')
    const density = tile('Density')
    expect(density).toHaveTextContent('—')
    expect(density).toHaveTextContent('No print has a length yet')
    expect(density).not.toHaveTextContent('The type')
  })

  it("names the type's density when the prints have none yet (#7685)", () => {
    renderPage(<SpoolMetricsPanel history={history({ used_m: null, implied_density_g_cm3: null, density_g_cm3: 1.24, density_mismatch: false })} gramsLeft={820} createdAt="2026-10-01T09:00:00Z" />)
    expect(tile('Density')).toHaveTextContent('No print has a length yet')
    expect(tile('Density')).toHaveTextContent('The type says 1.24 g/cm³')
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
