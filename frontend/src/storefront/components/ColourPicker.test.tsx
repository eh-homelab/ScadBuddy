import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Filament, PublicParam } from '../api/client'
import { colourSwatches } from '../colours'
import { ColourPicker } from './ColourPicker'

const f = (id: string, colour: string, colour_name: string | null, material = 'PLA'): Filament => ({ id, colour, colour_name, material })

const FILAMENTS = [
  f('b1', '#0A2E8C', 'Blue'),
  f('b2', '#3C6EDB', 'Blue'),
  f('o1', '#E8532F', 'Orange'),
  f('o2', '#E8532F', 'Orange', 'PETG'),
  f('t1', '#1C8C8C', 'Teal', 'PETG'),
  f('s1', '#FFFFFF', 'Natural', 'PVA'),
]

const param = (filaments: readonly Filament[]): PublicParam => ({
  kind: 'choice',
  label: 'Letters',
  group: '',
  colour: true,
  choices: [...new Set(filaments.map((filament) => filament.colour))],
})

function show(props: Partial<Parameters<typeof ColourPicker>[0]> & { filaments?: readonly Filament[] } = {}) {
  const onChange = vi.fn()
  const user = userEvent.setup()
  const { filaments = FILAMENTS, ...rest } = props
  render(<ColourPicker label="Letters" swatches={colourSwatches(param(filaments), filaments)} picked={false} shown="#E8532F" onChange={onChange} {...rest} />)
  return { user, onChange }
}

describe('ColourPicker (#6152, #6258)', () => {
  it('shows the shop’s choice until the customer picks, then hands back the colour', async () => {
    const { user, onChange } = show({ shown: '#0A2E8C' })
    const trigger = screen.getByRole('button', { name: /Letters/ })
    expect(trigger).toHaveTextContent('Blue')
    await user.click(trigger)
    await user.click(screen.getByRole('radio', { name: 'Orange' }))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ colour: '#E8532F' }))
  })

  it('shows one swatch per colour across materials, and never a material, closed or open (#6258)', async () => {
    const { user } = show({ shown: '#1C8C8C' })
    const trigger = screen.getByRole('button', { name: /Letters/ })
    expect(trigger).toHaveAccessibleName('Letters Teal')
    expect(document.body).not.toHaveTextContent(/PLA|PETG/)
    await user.click(trigger)
    expect(screen.getAllByRole('radio', { name: 'Orange' })).toHaveLength(1)
    expect(screen.queryByRole('group', { name: 'Material' })).toBeNull()
    expect(document.body).not.toHaveTextContent(/PLA|PETG/)
    for (const radio of screen.getAllByRole('radio')) expect(radio.getAttribute('aria-label')).not.toMatch(/PLA|PETG/)
  })

  it('tells two colours of one name apart by their hex', async () => {
    const { user } = show()
    await user.click(screen.getByRole('button', { name: /Letters/ }))
    expect(screen.getByRole('radio', { name: 'Blue #0A2E8C' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Blue #3C6EDB' })).toBeInTheDocument()
  })

  it('never offers a support material', async () => {
    const { user } = show()
    await user.click(screen.getByRole('button', { name: /Letters/ }))
    expect(screen.queryByRole('radio', { name: /Natural/ })).toBeNull()
  })

  it('searches a long list, and moves through the grid with the arrow keys', async () => {
    const many = Array.from({ length: 30 }, (_, n) => f(`m${n}`, `#${String(n).padStart(2, '0')}0000`, `Shade ${n}`))
    const { user } = show({ filaments: many, shown: undefined })
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
    const { user, onChange } = show({ picked: true, shown: '#0A2E8C' })
    await user.click(screen.getByRole('button', { name: /Letters/ }))
    await user.click(screen.getByRole('button', { name: 'Use the shop’s choice' }))
    expect(onChange).toHaveBeenCalledWith(undefined)
  })
})
