import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Filament } from '../api/client'
import { ColourPicker } from './ColourPicker'

const f = (id: string, colour: string, colour_name: string | null, material = 'PLA'): Filament => ({ id, colour, colour_name, material })

const FILAMENTS = [
  f('b1', '#0A2E8C', 'Blue'),
  f('b2', '#3C6EDB', 'Blue'),
  f('o1', '#E8532F', 'Orange'),
  f('o2', '#E8532F', 'Orange', 'PETG'),
  f('s1', '#FFFFFF', 'Natural', 'PVA'),
]

function show(props: Partial<Parameters<typeof ColourPicker>[0]> = {}) {
  const onChange = vi.fn()
  const user = userEvent.setup()
  render(<ColourPicker label="Letters" filaments={FILAMENTS} value={undefined} fallback={FILAMENTS[2]} onChange={onChange} {...props} />)
  return { user, onChange }
}

describe('ColourPicker (#6152)', () => {
  it('shows the shop’s choice until the customer picks, then hands back the filament', async () => {
    const { user, onChange } = show()
    const trigger = screen.getByRole('button', { name: /Letters/ })
    expect(trigger).toHaveTextContent('Orange')
    expect(trigger).toHaveTextContent('PLA')
    await user.click(trigger)
    await user.click(screen.getByRole('radio', { name: 'Orange PETG' }))
    expect(onChange).toHaveBeenCalledWith(FILAMENTS[3])
  })

  it('tells two filaments of one name apart by their hex', async () => {
    const { user } = show()
    await user.click(screen.getByRole('button', { name: /Letters/ }))
    expect(screen.getByRole('radio', { name: 'Blue #0A2E8C PLA' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Blue #3C6EDB PLA' })).toBeInTheDocument()
    // One name in two materials is two choices, each named once.
    expect(screen.getByRole('radio', { name: 'Orange PLA' })).toBeInTheDocument()
  })

  it('never offers a support material', async () => {
    const { user } = show()
    await user.click(screen.getByRole('button', { name: /Letters/ }))
    expect(screen.queryByRole('radio', { name: /Natural/ })).toBeNull()
    const materials = screen.getByRole('group', { name: 'Material' })
    expect(within(materials).queryByRole('button', { name: 'PVA' })).toBeNull()
  })

  it('filters by material', async () => {
    const { user } = show()
    await user.click(screen.getByRole('button', { name: /Letters/ }))
    await user.click(screen.getByRole('button', { name: 'PETG' }))
    expect(screen.getAllByRole('radio').map((radio) => radio.getAttribute('aria-label'))).toEqual(['Orange PETG'])
  })

  it('searches a long list, and moves through the grid with the arrow keys', async () => {
    const many = Array.from({ length: 30 }, (_, n) => f(`m${n}`, `#${String(n).padStart(2, '0')}0000`, `Shade ${n}`))
    const { user } = show({ filaments: many, fallback: undefined })
    const trigger = screen.getByRole('button', { name: /Letters/ })
    expect(trigger).toHaveTextContent('Choose a colour')
    await user.click(trigger)
    await user.type(screen.getByRole('searchbox', { name: 'Find a colour' }), 'Shade 2')
    expect(screen.getAllByRole('radio')).toHaveLength(11)
    await user.clear(screen.getByRole('searchbox', { name: 'Find a colour' }))
    screen.getAllByRole('radio')[0]!.focus()
    await user.keyboard('{ArrowRight}{ArrowDown}')
    expect(document.activeElement).toBe(screen.getAllByRole('radio')[5])
  })

  it('goes back to the shop’s choice', async () => {
    const { user, onChange } = show({ value: 'b1' })
    await user.click(screen.getByRole('button', { name: /Letters/ }))
    await user.click(screen.getByRole('button', { name: 'Use the shop’s choice' }))
    expect(onChange).toHaveBeenCalledWith(undefined)
  })
})
