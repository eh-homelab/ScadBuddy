import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { FilterBar } from './FilterBar'
import { CHIPS_SHOWN, shownChips } from './filterChips'

describe('FilterBar (#4249)', () => {
  it('selects chips, one or several, and clears', async () => {
    const onStatus = vi.fn()
    const onKind = vi.fn()
    const onClear = vi.fn()
    const { user } = renderPage(
      <FilterBar
        label="Order filters"
        filtered
        onClear={onClear}
        chips={[
          {
            key: 'status',
            label: 'Status',
            chips: [
              { value: 'draft', label: 'Draft', count: 2 },
              { value: 'quoted', label: 'Quoted' },
            ],
            selected: ['draft'],
            onChange: onStatus,
          },
          { key: 'kind', label: 'Kind', multiple: true, chips: [{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }], selected: ['a'], onChange: onKind },
        ]}
      />,
    )
    expect(screen.getByRole('search', { name: 'Order filters' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Draft/ })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: /Draft/ })).toHaveTextContent('2')
    await user.click(screen.getByRole('radio', { name: 'Quoted' }))
    expect(onStatus).toHaveBeenLastCalledWith(['quoted'])
    await user.click(screen.getByRole('radio', { name: /Draft/ }))
    expect(onStatus).toHaveBeenLastCalledWith([])
    await user.click(screen.getByRole('button', { name: 'B' }))
    expect(onKind).toHaveBeenLastCalledWith(['a', 'b'])
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(onClear).toHaveBeenCalled()
  })

  it('searches after a pause and offers the selects', async () => {
    const onSearch = vi.fn()
    const onSince = vi.fn()
    const { user } = renderPage(
      <FilterBar
        label="Order filters"
        search={{ value: '', onChange: onSearch, placeholder: 'Search orders' }}
        selects={[{ key: 'since', label: 'Since', value: 'any', options: [{ value: 'any', label: 'Any time' }, { value: '7d', label: 'Last 7 days' }], onChange: onSince }]}
      />,
    )
    await user.type(screen.getByRole('searchbox', { name: 'Search orders' }), 'ada')
    await waitFor(() => expect(onSearch).toHaveBeenCalledWith('ada'))
    await choose(user, screen.getByRole('combobox', { name: 'Since' }), 'Last 7 days')
    expect(onSince).toHaveBeenCalledWith('7d')
  })

  it('shows the first chips and every selected one, with a toggle for the rest', async () => {
    const chips = Array.from({ length: CHIPS_SHOWN + 3 }, (_, i) => ({ value: `v${i}`, label: `V${i}` }))
    expect(shownChips(chips, (c) => c.value, [`v${CHIPS_SHOWN + 2}`], false).map((c) => c.value)).toEqual([
      ...chips.slice(0, CHIPS_SHOWN).map((c) => c.value),
      `v${CHIPS_SHOWN + 2}`,
    ])
    const { user } = renderPage(
      <FilterBar label="Filters" chips={[{ key: 'k', label: 'Kind', chips, selected: [], onChange: () => {} }]} />,
    )
    expect(screen.getAllByRole('radio')).toHaveLength(CHIPS_SHOWN)
    await user.click(screen.getByRole('button', { name: 'Kind: show 3 more' }))
    expect(screen.getAllByRole('radio')).toHaveLength(CHIPS_SHOWN + 3)
    await user.click(screen.getByRole('button', { name: 'Kind: show fewer' }))
    expect(screen.getAllByRole('radio')).toHaveLength(CHIPS_SHOWN)
  })
})
