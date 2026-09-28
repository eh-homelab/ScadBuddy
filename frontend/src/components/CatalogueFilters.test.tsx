import { act, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { DEFAULT_QUERY, type CatalogueQuery } from '../lib/catalogueQuery'
import { CatalogueFilters } from './CatalogueFilters'

const TAGS = [
  { tag: 'keychain', count: 2 },
  { tag: 'Tea & Coffee', count: 1 },
]

function setup(query: Partial<CatalogueQuery> = {}) {
  const onChange = vi.fn()
  const user = userEvent.setup()
  const props = { tags: TAGS, shown: 3, total: 4, onChange }
  const view = render(<CatalogueFilters query={{ ...DEFAULT_QUERY, ...query }} {...props} />)
  const rerender = (next: Partial<CatalogueQuery>) =>
    view.rerender(<CatalogueFilters query={{ ...DEFAULT_QUERY, ...next }} {...props} />)
  return { onChange, user, rerender }
}


/** Holds the query the way the page does, so a change reaches the component again. */
function Stateful({ initial, onChange }: { initial: CatalogueQuery; onChange: (q: CatalogueQuery) => void }) {
  const [query, setQuery] = useState(initial)
  return (
    <CatalogueFilters
      query={query}
      tags={TAGS}
      shown={1}
      total={4}
      onChange={(next) => {
        onChange(next)
        setQuery(next)
      }}
    />
  )
}

const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 400)))

describe('CatalogueFilters', () => {
  it('reports the search once typing settles, replacing the history entry', async () => {
    const { onChange, user } = setup()
    await user.type(screen.getByRole('searchbox', { name: 'Search models' }), 'crème')

    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(onChange).toHaveBeenCalledOnce()
    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_QUERY, q: 'crème' }, { replace: true })
  })

  it('shows a search that changes from outside, as back/forward does', () => {
    const { rerender } = setup({ q: 'grid' })
    expect(screen.getByRole('searchbox')).toHaveValue('grid')
    rerender({ q: '' })
    expect(screen.getByRole('searchbox')).toHaveValue('')
  })

  it('focuses the search on "/", but not while typing in another field', async () => {
    const { user } = setup()
    const other = document.body.appendChild(document.createElement('input'))

    await user.keyboard('/')
    const search = screen.getByRole('searchbox')
    expect(search).toHaveFocus()
    expect(search).toHaveValue('')

    other.focus()
    await user.keyboard('/')
    expect(other).toHaveFocus()
    expect(other).toHaveValue('/')
    other.remove()
  })

  it('toggles a tag chip, with its count', async () => {
    const { onChange, user } = setup({ tags: ['keychain'] })
    const on = screen.getByRole('button', { name: 'keychain 2' })
    expect(on).toHaveAttribute('aria-pressed', 'true')
    await user.click(on)
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, tags: [] })

    const off = screen.getByRole('button', { name: 'Tea & Coffee 1' })
    expect(off).toHaveAttribute('aria-pressed', 'false')
    await user.click(off)
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, tags: ['keychain', 'Tea & Coffee'] })
  })

  it('sets the origin and the sort', async () => {
    const { onChange, user } = setup()
    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'Built-in' }))
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, origin: 'builtin' })

    await user.selectOptions(screen.getByRole('combobox', { name: 'Sort' }), 'Name')
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, sort: 'name' })
  })

  it('counts the results and clears the filters, keeping sort and view', async () => {
    const { onChange, user, rerender } = setup()
    expect(screen.getByTestId('result-count')).toHaveTextContent('3 of 4')
    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument()

    rerender({ q: 'x', tags: ['keychain'], origin: 'mine', sort: 'name', view: 'list' })
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, sort: 'name', view: 'list' })
  })

  it('does not bring back a pending search after "Clear filters"', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(<Stateful initial={{ ...DEFAULT_QUERY, tags: ['keychain'] }} onChange={onChange} />)

    await user.type(screen.getByRole('searchbox'), 'ab')
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await settle()

    expect(onChange).toHaveBeenCalledOnce()
    expect(onChange).toHaveBeenLastCalledWith(DEFAULT_QUERY)
    expect(screen.getByRole('searchbox')).toHaveValue('')
  })

  it('carries a pending search into a tag toggle rather than losing or re-applying it', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(<Stateful initial={DEFAULT_QUERY} onChange={onChange} />)

    await user.type(screen.getByRole('searchbox'), 'ab')
    await user.click(screen.getByRole('button', { name: 'keychain 2' }))
    await settle()

    expect(onChange).toHaveBeenCalledOnce()
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, q: 'ab', tags: ['keychain'] })
    expect(screen.getByRole('searchbox')).toHaveValue('ab')
  })

  it('drops a pending search when the filters change from outside, as back/forward does', async () => {
    const { onChange, user, rerender } = setup()
    await user.type(screen.getByRole('searchbox'), 'ab')
    rerender({ origin: 'mine' })
    await settle()

    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('searchbox')).toHaveValue('')
  })

  it('leaves "/" alone while a modal dialog is open', async () => {
    const { user } = setup()
    const dialog = document.body.appendChild(document.createElement('div'))
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')
    dialog.tabIndex = -1
    dialog.focus()

    await user.keyboard('/')
    expect(screen.getByRole('searchbox')).not.toHaveFocus()
    dialog.remove()
  })
})
