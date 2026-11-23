import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'
import { spoolId, T } from '../../mocks/features/filament/seed'
import { PICKER_FILTERS_KEY } from '../../lib/filamentPickerFilters'
import { renderPage } from '../../test/utils'
import { FilamentQuickFind } from './FilamentQuickFind'
import { FilamentSearchBox } from './FilamentSearchBox'

// #6894: the quick find over the spools and types, on the Filament page.

function Here() {
  const location = useLocation()
  return (
    <>
      <output data-testid="here">{location.pathname}</output>
      <output data-testid="back">{JSON.stringify(location.state)}</output>
    </>
  )
}

function render(route = '/filament/spools') {
  return renderPage(
    <Routes>
      <Route
        path="*"
        element={
          <>
            <FilamentQuickFind />
            <textarea aria-label="Composer" />
            <FilamentSearchBox value="" onChange={() => {}} label="Search spools" placeholder="Search" />
            <Here />
          </>
        }
      />
    </Routes>,
    { route },
  )
}

const list = () => screen.findByRole('listbox', { name: 'Spools and types' })
const options = async () => within(await list()).getAllByRole('option')

afterEach(() => localStorage.removeItem(PICKER_FILTERS_KEY))

describe('the filament quick find', () => {
  it('opens on Ctrl+K and opens spool #4 on Enter', async () => {
    const { user } = render()
    await user.keyboard('{Control>}k{/Control}')
    const input = await screen.findByRole('combobox')
    await user.type(input, '#4')
    await waitFor(async () => expect((await options()).map((one) => one.textContent)).toEqual([expect.stringContaining('#4 ·')]))
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByTestId('here').textContent).toBe(`/filament/spools/${spoolId(4)}`))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens from its button and finds spools and types together, in groups', async () => {
    const { user } = render()
    await user.click(screen.getByRole('button', { name: /Find filament/ }))
    await user.type(await screen.findByRole('combobox'), 'elegoo gold')
    const listbox = await list()
    await waitFor(() => expect(within(listbox).getAllByRole('option')).toHaveLength(2))
    expect(within(listbox).getByRole('group', { name: 'Spools' })).toHaveTextContent('#9')
    expect(within(listbox).getByRole('group', { name: 'Types' })).toHaveTextContent('EL-PLA-SILK-GOLD')
    await user.keyboard('{ArrowDown}{Enter}')
    await waitFor(() => expect(screen.getByTestId('here').textContent).toBe(`/filament/types/${T.GOLD}`))
  })

  it('finds by hex without # and says when nothing matches', async () => {
    const { user } = render()
    await user.keyboard('{Control>}k{/Control}')
    const input = await screen.findByRole('combobox')
    await user.type(input, 'ff6a13')
    await waitFor(async () => expect(await options()).toHaveLength(2))
    await user.clear(input)
    await user.type(input, 'nothing-like-this')
    expect(await screen.findByText('No spool or type matches “nothing-like-this”.')).toBeInTheDocument()
  })

  it('shares the colour picker remembered loaded-only filter', async () => {
    localStorage.setItem(PICKER_FILTERS_KEY, JSON.stringify({ material: null, loadedOnly: true }))
    const { user } = render()
    await user.keyboard('{Control>}k{/Control}')
    const box = await screen.findByRole('checkbox', { name: 'Loaded in a printer' })
    expect(box).toBeChecked()
    // Spools #8 and #4 are in trays, the fuller first; their types count as loaded.
    await waitFor(async () => expect((await options()).map((one) => /#(\d+) ·/.exec(one.textContent ?? '')?.[1] ?? one.textContent)).toEqual(['8', '4', expect.stringContaining('BL-PLA-BASIC-BLACK'), expect.stringContaining('INS-PETG-CLEAR')]))
    await user.click(box)
    expect(JSON.parse(localStorage.getItem(PICKER_FILTERS_KEY)!)).toEqual({ material: null, loadedOnly: false })
  })

  it('leaves Ctrl+K to another field the person is typing in', async () => {
    const { user } = render()
    const composer = screen.getByRole('textbox', { name: 'Composer' })
    await user.click(composer)
    const event = new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true })
    composer.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens on Ctrl+K from the tab\'s own search box', async () => {
    const { user } = render()
    await user.click(screen.getByRole('searchbox', { name: 'Search spools' }))
    await user.keyboard('{Control>}k{/Control}')
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('hands the open tab search back only on the Spools and Types tabs', async () => {
    const spools = render('/filament/spools?q=gold')
    await spools.user.keyboard('{Control>}k{/Control}')
    await spools.user.type(await screen.findByRole('combobox'), '#4')
    await waitFor(async () => expect(await options()).toHaveLength(1))
    await spools.user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByTestId('back').textContent).toBe('{"back":"?q=gold"}'))
    spools.unmount()

    const ledger = render('/filament/ledger?since=2026-01-01')
    await ledger.user.keyboard('{Control>}k{/Control}')
    await ledger.user.type(await screen.findByRole('combobox'), '#4')
    await waitFor(async () => expect(await options()).toHaveLength(1))
    await ledger.user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByTestId('here').textContent).toBe(`/filament/spools/${spoolId(4)}`))
    expect(screen.getByTestId('back').textContent).toBe('null')
  })
})
