import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { BambuFields } from './BambuFields'

// The owner's spool read 2026-11-26 (#7905), as the server decodes it.
const read = {
  complete: false,
  missing_blocks: Array.from({ length: 38 }, (_, i) => i + 13),
  variant_id: 'A17-P1',
  material_id: 'GFA17',
  filament_type: 'PLA',
  detailed_filament_type: 'PLA Translucent',
  color: '#B8ACD680',
  spool_weight_g: 1000,
  filament_diameter_mm: 1.75,
  drying_temperature_c: 55,
  drying_time_h: 8,
  hotend_min_temperature_c: 200,
  hotend_max_temperature_c: 240,
  tray_uid: '6DB40AFAF762423DBD73428BDA79DCD0',
  production_datetime: '2026-06-23T05:03:00',
}

describe('BambuFields', () => {
  it('lists the fields the phone read and says how much it read', () => {
    render(<BambuFields bambu={read} />)
    const panel = screen.getByRole('region', { name: 'On the Bambu tag' })
    for (const text of ['PLA Translucent', 'GFA17 / A17-P1', '#B8ACD680', '1000 g', '1.75 mm', '200–240 °C', '55 °C for 8 h', '2026-06-23 05:03', '6DB40AFAF762423DBD73428BDA79DCD0']) {
      expect(panel).toHaveTextContent(text)
    }
    expect(panel).toHaveTextContent('The phone read 10 of 48 blocks.')
  })

  it('says nothing of missing blocks for a complete read', () => {
    render(<BambuFields bambu={{ ...read, complete: true, missing_blocks: [] }} />)
    expect(screen.queryByText(/The phone read/)).toBeNull()
  })
})
