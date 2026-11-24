import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { postTagRead } from '../../../api/filament'
import { spoolId } from '../../../mocks/features/filament/seed'
import { filamentLinks } from '../../../lib/filamentLinks'
import { renderPage } from '../../../test/utils'
import { KnownTag } from './KnownTag'

describe('KnownTag (#7273)', () => {
  it('shows the spool card, the photos, the weight, then the links in that order', async () => {
    const result = await postTagRead({ uid: '04:a1:b2:c3:d4:e5:f6', records: [] })
    renderPage(<KnownTag spool={result.spool!} />)
    const card = screen.getByRole('region', { name: 'Spool #3' })
    expect(within(card).getByText(/^PLA/)).toBeInTheDocument()
    expect(within(card).getByText('930 g left')).toBeInTheDocument()
    const filament = screen.getByRole('group', { name: 'Filament label' })
    const weight = await screen.findByLabelText('Gross weight (g)')
    const openSpool = screen.getByRole('link', { name: 'Open spool' })
    expect(openSpool).toHaveAttribute('href', filamentLinks.spool(spoolId(3)))
    expect(screen.getByRole('button', { name: 'Write tag' })).toBeDisabled()
    const order = [card, filament, weight, openSpool]
    for (let at = 1; at < order.length; at += 1) {
      expect(order[at - 1]!.compareDocumentPosition(order[at]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })
})
