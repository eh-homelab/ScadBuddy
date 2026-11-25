import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { InsertWeightCheck } from '../../api/filament'
import { BelowLabelChoice } from './BelowLabelChoice'

// #7683 — a new spool's reading under its label weight: the three readings of it.

function below(reading: number, label = 1000, empty = 200): InsertWeightCheck {
  const filament = reading - empty
  const resolutions: InsertWeightCheck['resolutions'] = [{ kind: 'tared', filament_g: reading, new_label_weight_g: null }]
  if (filament >= 0) {
    resolutions.push({ kind: 'short', filament_g: filament, new_label_weight_g: null })
    resolutions.push({ kind: 'label_wrong', filament_g: filament, new_label_weight_g: Math.max(1, Math.floor(filament + 0.5)) })
  }
  const shortfall = label - reading
  return {
    offer: null,
    reason: 'below_label',
    insert_type_id: 'insert',
    empty_weight_g: empty,
    filament_g: filament,
    shortfall_g: shortfall,
    resolutions,
    default: shortfall > 0.1 * label && resolutions.length > 1 ? 'short' : 'tared',
  }
}

describe('BelowLabelChoice', () => {
  it('lists the three readings with their numbers and the default checked', () => {
    render(<BelowLabelChoice check={below(900)} value="tared" onChange={() => {}} />)
    expect(screen.getByRole('group', { name: 'The reading is 100 g under the label weight. What is up?' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'The scale was tared on the empty spool: 900 g of filament' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'The spool is short: 700 g of filament, 100 g under its label' })).not.toBeChecked()
    expect(screen.getByRole('radio', { name: 'The label weight is wrong: 700 g of filament, and the label becomes 700 g' })).toBeInTheDocument()
  })

  it('lists only tared under the empty weight', () => {
    render(<BelowLabelChoice check={below(150)} value="tared" onChange={() => {}} />)
    expect(screen.getAllByRole('radio')).toHaveLength(1)
    expect(screen.getByRole('radio', { name: 'The scale was tared on the empty spool: 150 g of filament' })).toBeChecked()
  })

  it('tells the chosen reading', async () => {
    const chosen: string[] = []
    const { user } = await import('../../test/utils').then(({ renderPage }) => renderPage(<BelowLabelChoice check={below(800)} value="short" onChange={(next) => chosen.push(next)} />))
    expect(screen.getByRole('radio', { name: /The spool is short/ })).toBeChecked()
    await user.click(screen.getByRole('radio', { name: /The label weight is wrong/ }))
    expect(chosen).toEqual(['label_wrong'])
  })
})
