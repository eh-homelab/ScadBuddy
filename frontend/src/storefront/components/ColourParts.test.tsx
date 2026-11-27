import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { CHARM, DOLLHOUSE } from '../../mocks/features/storefrontPublic'
import type { Listing, ParamValue } from '../api/client'
import { ListingView } from './ListingView'

function Stateful({ listing, onChange }: { listing: Listing; onChange: (name: string, next: ParamValue | undefined) => void }) {
  const [values, setValues] = useState<Record<string, ParamValue>>({})
  return (
    <ListingView
      listing={listing}
      values={values}
      onChange={(name, next) => {
        onChange(name, next)
        setValues(({ [name]: _old, ...rest }) => (next === undefined ? rest : { ...rest, [name]: next }))
      }}
    />
  )
}

const FLOORS = ['rug_color', 'floor_color', 'rug_border_color', 'floor_accent_color']

describe('ColourParts (#8127)', () => {
  it('shows a long list in its groups, the first open, and says the shop’s choice once', () => {
    render(<Stateful listing={DOLLHOUSE} onChange={vi.fn()} />)
    const groups = screen.getAllByRole('button').filter((button) => button.hasAttribute('aria-expanded') && button.hasAttribute('aria-controls'))
    expect(groups.map((group) => [group.textContent, group.getAttribute('aria-expanded')])).toEqual([
      ['Floors colours4 parts', 'true'],
      ['Roof, stairs & railing colours7 parts', 'false'],
      ['Windows & doors colours10 parts', 'false'],
      ['Walls outside colours4 parts', 'false'],
      ['Walls inside colours6 parts', 'false'],
    ])
    expect(screen.getAllByText("Each part is the shop's choice, unless you pick one.")).toHaveLength(1)
    // Only the open group's parts are rows: the four floors and their "All 4 parts".
    expect(screen.getByRole('button', { name: 'Rug Jade White · Basic' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Roof Jade/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'All 4 parts Jade White · Basic' })).toBeInTheDocument()
  })

  it('opens and closes a group', async () => {
    const user = userEvent.setup()
    render(<Stateful listing={DOLLHOUSE} onChange={vi.fn()} />)
    const roof = screen.getByRole('button', { name: /Roof, stairs & railing colours/ })
    await user.click(roof)
    expect(roof).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Roof Jade White · Basic' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Floors colours/ }))
    expect(screen.queryByRole('button', { name: /^Rug Jade/ })).toBeNull()
  })

  it('sets one colour on every part of a group, and each part can still take its own', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Stateful listing={DOLLHOUSE} onChange={onChange} />)
    await user.click(screen.getByRole('button', { name: /^All 4 parts/ }))
    await user.click(screen.getByRole('radio', { name: 'Caramel · Basic' }))
    expect(onChange.mock.calls).toEqual(FLOORS.map((name) => [name, '#C98E62']))
    expect(screen.getByRole('button', { name: /^All 4 parts/ })).toHaveAccessibleName('All 4 parts Caramel · Basic')

    await user.click(screen.getByRole('button', { name: /^Rug border/ }))
    await user.click(screen.getByRole('radio', { name: 'Black · Basic' }))
    expect(screen.getByRole('button', { name: /^Rug border/ })).toHaveAccessibleName('Rug border Black · Basic')
    expect(screen.getByRole('button', { name: /^Rug Caramel/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^All 4 parts/ })).toHaveAccessibleName('All 4 parts Mixed colours')

    await user.click(screen.getByRole('button', { name: /^All 4 parts/ }))
    await user.click(screen.getByRole('button', { name: 'Use the shop’s choice for all' }))
    expect(onChange.mock.calls.slice(-4)).toEqual(FLOORS.map((name) => [name, undefined]))
  })

  it('offers the colours the design uses first, then the rest', async () => {
    const user = userEvent.setup()
    render(<Stateful listing={DOLLHOUSE} onChange={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /^Rug Jade/ }))
    await user.click(screen.getByRole('radio', { name: 'Caramel · Basic' }))
    await user.click(screen.getByRole('button', { name: /^Floor Jade/ }))
    const grid = screen.getByRole('radiogroup', { name: 'Floor' })
    const radios = within(grid).getAllByRole('radio')
    expect(radios.slice(0, 2).map((radio) => radio.getAttribute('aria-label'))).toEqual(['Caramel · Basic', 'Jade White · Basic'])
    expect(within(grid).getByText('In this design')).toBeInTheDocument()
    expect(within(grid).getByText('More colours')).toBeInTheDocument()
    // The colour on the row has the focus, not the search.
    expect(document.activeElement).toBe(within(grid).getByRole('radio', { name: 'Jade White · Basic' }))
  })

  it('keeps a short list plain: no groups and no row for all parts', () => {
    render(<Stateful listing={CHARM} onChange={vi.fn()} />)
    expect(screen.getAllByRole('button').filter((button) => button.hasAttribute('aria-controls') && !button.hasAttribute('aria-haspopup'))).toHaveLength(0)
    expect(screen.queryByRole('button', { name: /^All \d+ parts/ })).toBeNull()
    expect(screen.getByRole('button', { name: /Body colour/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Rim colour/ })).toBeInTheDocument()
  })
})
