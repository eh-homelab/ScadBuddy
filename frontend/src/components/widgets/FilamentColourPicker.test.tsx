import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Param } from '../../api/types'
import { spoolColour } from '../../lib/filamentColour'
import { SPOOL_INVENTORY } from '../../mocks/features/spoolInventory'
import { server } from '../../mocks/server'
import { ColorWidget } from './ColorWidget'

const param: Param = { group: 'Colours', name: 'body_color', type: 'color', initial: '#1B6CA8', caption: 'Plate' }

function setup(value = '#1B6CA8') {
  const onChange = vi.fn()
  const user = userEvent.setup()
  render(<ColorWidget param={param} value={value} onChange={onChange} />)
  return { onChange, user }
}

describe('a colour from a filament (#1026)', () => {
  it("lists Bambuddy's spools, loaded first, and takes the chosen one's colour", async () => {
    let reads = 0
    server.use(
      http.get('/api/v1/farm/inventory', () => {
        reads += 1
        return undefined
      }),
    )
    const { onChange, user } = setup()
    // Not read until asked for.
    expect(reads).toBe(0)

    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    const list = await screen.findByRole('listbox', { name: 'Filaments for Plate' })
    const options = within(list).getAllByRole('option')
    expect(options.map((option) => option.textContent)).toEqual([
      'Insignia PETG — Clearclear, no tint recorded',
      'Bambu Lab PLA Basic — Mistletoe Green1000 g',
      'Bambu Lab PLA Basic — Hot Pink930 g',
    ])
    // A clear spool with no tint has no colour to give.
    expect(options[0]).toHaveAttribute('aria-disabled', 'true')

    await user.click(within(list).getByRole('option', { name: /Hot Pink/ }))
    expect(onChange).toHaveBeenCalledWith('#F5547C')
    expect(screen.queryByRole('listbox', { name: 'Filaments for Plate' })).toBeNull()
  })

  it('marks the spool whose colour the parameter already has', async () => {
    const { user } = setup('#3F8E43')
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    expect(await screen.findByRole('option', { name: /Mistletoe Green/ })).toHaveTextContent('(the colour now)')
    expect(screen.getByRole('option', { name: /Hot Pink/ })).not.toHaveTextContent('(the colour now)')
  })

  it("names a loaded spool's printer, AMS and slot, and lists it ahead of a fuller one on the shelf (#2779 review)", async () => {
    server.use(
      http.get('/api/v1/farm/inventory', () =>
        HttpResponse.json({
          spools: [
            { ...SPOOL_INVENTORY[0], id: 50, color_name: 'Shelf Blue', rgba: '0086D6FF', remaining_g: 1000 },
            {
              ...SPOOL_INVENTORY[0],
              id: 51,
              color_name: 'Loaded Orange',
              rgba: 'FF6A13FF',
              remaining_g: 120,
              loaded: { printer_id: 2, printer_name: 'H2C-East', ams_id: 1, tray_id: 0 },
            },
          ],
          slots: [],
        }),
      ),
    )
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    const list = await screen.findByRole('listbox', { name: 'Filaments for Plate' })
    expect(within(list).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Bambu Lab PLA Basic — Loaded OrangeH2C-East · AMS-B · slot 1 · 120 g',
      'Bambu Lab PLA Basic — Shelf Blue1000 g',
    ])
  })

  it('draws a clear spool as clear, never black (#2779 review)', async () => {
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    const clear = await screen.findByRole('option', { name: /Clear/ })
    expect(within(clear).getByTestId('clear-swatch')).toBeInTheDocument()
    expect(clear.querySelector('[title="#000000"]')).toBeNull()
  })

  it('drops a read that answers after the list was closed (#2779 review)', async () => {
    let answer = () => {}
    server.use(
      http.get('/api/v1/farm/inventory', async () => {
        await new Promise<void>((resolve) => (answer = resolve))
        // An explicit list, not a fall-through to the default handler, so what this
        // test checks cannot change with that handler.
        return HttpResponse.json({ spools: SPOOL_INVENTORY.filter((spool) => !spool.archived), slots: [] })
      }),
    )
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    await user.keyboard('{Escape}')

    answer()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByRole('listbox', { name: 'Filaments for Plate' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Choose Plate' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('says why when Bambuddy cannot be read', async () => {
    server.use(
      http.get('/api/v1/farm/inventory', () =>
        HttpResponse.json({ detail: 'Bambuddy is not configured' }, { status: 409 }),
      ),
    )
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy is not configured')
  })
})

describe('spoolColour', () => {
  it.each([
    ['F5547CFF', { hex: '#F5547C', translucent: false }],
    ['77EDD780', { hex: '#77EDD7', translucent: true }],
    ['#0086D6', { hex: '#0086D6', translucent: false }],
    ['00000000', null],
    ['nonsense', null],
    [null, null],
  ])('%s', (rgba, colour) => {
    expect(spoolColour({ rgba })).toEqual(colour)
  })
})
