import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Spool } from '../../api/filament'
import { NeedsType } from './SpoolsTable'

const spool = (type_id: string | null, material: string) => ({ id: 's', number: 3, type_id, names: { material } }) as unknown as Spool

const show = (one: Spool) => render(<NeedsType spool={one} />, { wrapper: MemoryRouter })

describe('the type badge', () => {
  it('says "Needs type" for a spool with no type', () => {
    show(spool(null, 'PLA'))
    expect(screen.getByText('Needs type')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Set type for spool #3' })).toBeInTheDocument()
  })

  it('says "Unknown material" for a spool with a type, with the same Set type action', () => {
    show(spool('t1', 'Unknown'))
    expect(screen.getByText('Unknown material')).toBeInTheDocument()
    expect(screen.queryByText('Needs type')).toBeNull()
    expect(screen.getByRole('link', { name: 'Set type for spool #3' })).toBeInTheDocument()
  })

  it('shows nothing for a typed spool with a known material', () => {
    const { container } = show(spool('t1', 'PLA'))
    expect(container).toBeEmptyDOMElement()
  })
})
