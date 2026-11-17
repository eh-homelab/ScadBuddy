import { useState } from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { FilamentInventorySpool, Param, TrayPlace } from '../../api/types'
import { spoolColour } from '../../lib/filamentColour'
import { inventoryRows } from '../../mocks/features/filament'
import { sbSpool } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { ColorWidget } from './ColorWidget'

const param: Param = { group: 'Colours', name: 'body_color', type: 'color', initial: '#1B6CA8', caption: 'Plate' }

const ROUTE = '/api/v1/filament/inventory'

/** An SB spool of Bambu Lab PLA Basic with this number, colour name and `#RRGGBBAA`. */
function row(number: number, colour_name: string, rgba: string, grams_left = 800, loaded: TrayPlace | null = null): FilamentInventorySpool {
  const id = `62620000-0000-4000-8000-${String(number).padStart(12, '0')}`
  const spool = sbSpool({ id, number, material: 'PLA', subtype: 'Basic', brand: 'Bambu Lab', colour_name, rgba, grams_left, cost_per_kg: null })
  return { spool, loaded, bb_spool_id: null, presets: [] }
}

function inventory(...spools: FilamentInventorySpool[]) {
  return http.get(ROUTE, () => HttpResponse.json({ spools, trays: [] }))
}

function setup(value = '#1B6CA8') {
  const onChange = vi.fn()
  const user = userEvent.setup()
  render(<ColorWidget param={param} value={value} onChange={onChange} />)
  return { onChange, user }
}

describe('a colour from a filament (#1026)', () => {
  it("lists the seed's SB spools by number, loaded first, and takes the chosen one's colour (#6262 Task 9)", async () => {
    let reads = 0
    server.use(
      http.get(ROUTE, () => {
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
    // The Filament section's seed (`mocks/features/filament/seed.ts`, #6262 Task 14).
    expect(options.map((option) => option.textContent)).toEqual([
      'Insignia PETG — Clear#8 · clear, no tint recorded',
      'Bambu Lab PLA Basic — Black#4 · 3DP-31B-598 · AMS-A · slot 1 · 120 g',
      'Elegoo PLA Silk — Gold#9 · 1000 g',
      'Bambu Lab PLA Basic — Mistletoe Green#11 · 1000 g',
      'Bambu Lab PLA Basic — Hot Pink#3 · 930 g',
      'Bambu Lab PETG HF — Translucent Orange#2 · translucent · 820 g',
      'Polymaker PETG — Clear#5 · translucent · 700 g',
      'Bambu Lab PLA Basic — Jade White#1 · 640 g',
      'PLA — Teal#6 · 500 g',
      'Unknown#7 · 300 g',
      'Sunlu TPU 95A — Red#10 · 250 g',
    ])
    // A clear spool with no tint has no colour to give.
    expect(options[0]).toHaveAttribute('aria-disabled', 'true')

    await user.click(within(list).getByRole('option', { name: /Hot Pink/ }))
    expect(onChange).toHaveBeenCalledWith('#F5547C')
    expect(screen.queryByRole('listbox', { name: 'Filaments for Plate' })).toBeNull()
  })

  it("takes a translucent spool's alpha, and an opaque spool's colour drops it (owner, 2026-11-14)", async () => {
    server.use(inventory(row(60, 'Clear Tint', '#FFFFFF80', 900), row(61, 'White', '#FFFFFFFF', 800)))
    const first = setup('#1B6CA8')
    await first.user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    expect(await screen.findByRole('option', { name: /Clear Tint/ })).toHaveTextContent('translucent')
    await first.user.click(screen.getByRole('option', { name: /Clear Tint/ }))
    expect(first.onChange).toHaveBeenCalledWith('#FFFFFF80')
  })

  it('sends the opaque colour over the same colour with an alpha', async () => {
    server.use(inventory(row(61, 'White', '#FFFFFFFF', 800)))
    const { onChange, user } = setup('#FFFFFF80')
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    await user.click(await screen.findByRole('option', { name: /White/ }))
    expect(onChange).toHaveBeenCalledWith('#FFFFFF')
  })

  it('marks only the clear spool when the colour has its alpha, and the opaque one when not (#6243 review)', async () => {
    server.use(inventory(row(60, 'Clear Tint', '#FFFFFF80', 900), row(61, 'White', '#FFFFFFFF', 800)))
    const clear = setup('#FFFFFF80')
    await clear.user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    expect(await screen.findByRole('option', { name: /Clear Tint/ })).toHaveTextContent('(the colour now)')
    expect(screen.getByRole('option', { name: /White/ })).not.toHaveTextContent('(the colour now)')
  })

  it('keeps a typed alpha, and sends nothing for the same colour spelt otherwise (#6243 review)', async () => {
    // Controlled, as the form holds it: each sent value comes back as the value.
    const sent: string[] = []
    function Form() {
      const [value, setValue] = useState('#1B6CA8')
      return (
        <ColorWidget
          param={param}
          value={value}
          onChange={(next) => {
            sent.push(next)
            setValue(next)
          }}
        />
      )
    }
    const user = userEvent.setup()
    const { unmount } = render(<Form />)
    const field = screen.getByRole('textbox', { name: 'Plate' })
    await user.clear(field)
    await user.type(field, '#FFFFFF80')
    expect(sent.at(-1)).toBe('#FFFFFF80')
    expect(field).toHaveValue('#FFFFFF80')
    unmount()

    const { onChange, user: again } = setup('#ffffff80')
    await again.click(screen.getByRole('textbox', { name: 'Plate' }))
    await again.tab()
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Plate' })).toHaveValue('#FFFFFF80')
  })

  it("keeps a translucent value's alpha when Any colour picks a new tint (#6289 review)", async () => {
    const { onChange, user } = setup('#FFFFFF80')
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    fireEvent.change(screen.getByLabelText('Plate picker'), { target: { value: '#ff0000' } })
    expect(onChange).toHaveBeenCalledWith('#FF000080')
  })

  it('marks the spool whose colour the parameter already has', async () => {
    const { user } = setup('#3F8E43')
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    expect(await screen.findByRole('option', { name: /Mistletoe Green/ })).toHaveTextContent('(the colour now)')
    expect(screen.getByRole('option', { name: /Hot Pink/ })).not.toHaveTextContent('(the colour now)')
  })

  it("names a loaded spool's printer, AMS and slot, and lists it ahead of a fuller one on the shelf (#2779 review)", async () => {
    const east = { printer_id: '00000000-0000-4000-8000-0000000000e2', printer_name: 'H2C-East', bambuddy_printer_id: null, ams_id: 1, tray_id: 0 }
    server.use(inventory(row(50, 'Shelf Blue', '#0086D6FF', 1000), row(51, 'Loaded Orange', '#FF6A13FF', 120, east)))
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    const list = await screen.findByRole('listbox', { name: 'Filaments for Plate' })
    expect(within(list).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Bambu Lab PLA Basic — Loaded Orange#51 · H2C-East · AMS-B · slot 1 · 120 g',
      'Bambu Lab PLA Basic — Shelf Blue#50 · 1000 g',
    ])
  })

  it('draws a clear spool as clear, never black (#2779 review)', async () => {
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    const clear = await screen.findByRole('option', { name: /Insignia PETG — Clear/ })
    expect(within(clear).getByTestId('clear-swatch')).toBeInTheDocument()
    expect(clear.querySelector('[data-colour="#000000"]')).toBeNull()
  })

  it('drops a read that answers after the list was closed (#2779 review)', async () => {
    let answer = () => {}
    server.use(
      http.get(ROUTE, async () => {
        await new Promise<void>((resolve) => (answer = resolve))
        // An explicit list, not a fall-through to the default handler, so what this
        // test checks cannot change with that handler.
        return HttpResponse.json({ spools: inventoryRows(), trays: [] })
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

  it('says why when the spools cannot be read', async () => {
    server.use(http.get(ROUTE, () => HttpResponse.json({ detail: 'the database is not answering' }, { status: 503 })))
    const { user } = setup()
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('the database is not answering')
  })

  it('tells an opaque and a translucent spool of one hue apart, and marks the one of the alpha set (#6316)', async () => {
    server.use(inventory(row(70, 'Ice Blue', '#0086D6FF', 900), row(71, 'Ice Blue Glass', '#0086D640', 800)))
    const { user } = setup('#0086D640')
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    const glass = await screen.findByRole('option', { name: /Ice Blue Glass/ })
    expect(glass).toHaveTextContent('translucent')
    const opaque = screen.getByRole('option', { name: /Ice Blue#70/ })
    expect(opaque).not.toHaveTextContent('translucent')
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

// #3656: picking the swatch of the colour already set, however it was spelt, sends
// nothing, as the hex box does not.
describe('a filament of the colour already set', () => {
  it.each([
    ['in another letter case', '#f5547c', '#F5547CFF'],
    ['as a colour name', 'hotpink', '#FF69B4FF'],
  ])('is not sent again, the value spelt %s', async (_how, value, rgba) => {
    server.use(
      inventory(row(3, 'Hot Pink', rgba)),
    )
    const { onChange, user } = setup(value)
    await user.click(screen.getByRole('button', { name: 'Choose Plate' }))
    const list = await screen.findByRole('listbox', { name: 'Filaments for Plate' })
    await user.click(within(list).getByRole('option', { name: /Hot Pink/ }))
    expect(onChange).not.toHaveBeenCalled()
  })
})
