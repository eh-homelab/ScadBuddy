import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Spool } from '../../../api/filament'
import { renderPage } from '../../../test/utils'
import { fillable } from './invoiceLines'
import { SpoolMatchPicker } from './SpoolMatchPicker'

const open = { id: 'open', number: 3, type_id: null, cost_per_kg: null, invoice_line_id: null, archived_at: null, rgba: '#FF0000FF', colour_name: 'Red' } as Spool
const spent = { ...open, id: 'spent', number: 4, archived_at: '2026-11-12T08:00:00Z' } as Spool
const costed = { ...open, id: 'costed', number: 5, cost_per_kg: '21.40' } as Spool

describe('the spools a line may fill (#6707)', () => {
  it('offers a used-up (archived) spool, since it was still bought', () => {
    expect(fillable([costed, open, spent], '', new Set()).map((one) => one.id)).toEqual(['open', 'spent', 'costed'])
  })

  it('marks an archived spool "used up" in the picker', async () => {
    const { user } = renderPage(<SpoolMatchPicker line={1} candidates={[open, spent]} quantity={2} value={[]} onChange={() => {}} money={(amount) => amount} />)
    await user.click(screen.getByRole('button', { name: 'Fills existing spools, line 1' }))
    expect(screen.getByText('Spool #4').closest('label')).toHaveTextContent('used up')
    expect(screen.getByText('Spool #3').closest('label')).not.toHaveTextContent('used up')
  })
})
