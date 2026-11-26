import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Resolved } from '../../../api/filament'
import { filamentLinks } from '../../../lib/filamentLinks'
import { renderPage } from '../../../test/utils'
import { codeLabel } from '../../../lib/resolvedCode'
import { CodeCard } from './CodeCard'

const place: Resolved = { kind: 'location', id: 'loc-1', code: 'L-4', name: 'Slot 1 of AMS-A', archived: false }
const spool: Resolved = { kind: 'spool', id: 'sp-1', code: 'S-142', name: null, archived: false }

describe('CodeCard (#7273, the hand-held Scan mode)', () => {
  it('names a place by its code, with its name under it, and opens its page', () => {
    renderPage(<CodeCard resolved={place} />)
    const card = screen.getByRole('region', { name: 'Place L-4' })
    expect(within(card).getByRole('heading', { name: 'Place L-4' })).toBeInTheDocument()
    expect(within(card).getByText('Slot 1 of AMS-A')).toBeInTheDocument()
    expect(within(card).queryByText('Archived')).toBeNull()
    expect(within(card).getByRole('link', { name: 'Open' })).toHaveAttribute('href', filamentLinks.location('loc-1'))
  })

  it('names a spool by its code and opens its page', () => {
    renderPage(<CodeCard resolved={spool} />)
    const card = screen.getByRole('region', { name: 'Spool S-142' })
    expect(within(card).getByRole('link', { name: 'Open' })).toHaveAttribute('href', filamentLinks.spool('sp-1'))
  })

  it('says when the thing is archived', () => {
    renderPage(<CodeCard resolved={{ ...place, archived: true }} />)
    expect(within(screen.getByRole('region', { name: 'Place L-4' })).getByText('Archived')).toBeInTheDocument()
  })

  it('labels a thing by its kind and code', () => {
    expect(codeLabel(place)).toBe('Place L-4')
    expect(codeLabel(spool)).toBe('Spool S-142')
  })
})
