import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { RgbaColourField } from './RgbaColourField'

function setup(value: string | null, inherited: string | null = null) {
  const onChange = vi.fn()
  const user = userEvent.setup()
  const view = render(<RgbaColourField value={value} onChange={onChange} inherited={inherited} />)
  return { onChange, user, ...view }
}

/** The field as a form holds it: each change comes back as its value. */
function Held({ initial, onChange }: { initial: string | null; onChange: (rgba: string) => void }) {
  const [value, setValue] = useState(initial)
  return (
    <RgbaColourField
      value={value}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

describe('RgbaColourField', () => {
  it('"Translucent" sets alpha 80 and keeps the colour', async () => {
    const { onChange, user } = setup('#FF8800FF')
    await user.click(screen.getByRole('button', { name: 'Translucent' }))
    expect(onChange).toHaveBeenLastCalledWith('#FF880080')
  })

  it('"Clear" sets alpha 10 and "Opaque" sets FF', async () => {
    const { onChange, user } = setup('#FF880080')
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onChange).toHaveBeenLastCalledWith('#FF880010')
    await user.click(screen.getByRole('button', { name: 'Opaque' }))
    expect(onChange).toHaveBeenLastCalledWith('#FF8800FF')
  })

  it('marks the preset the value is in', () => {
    setup('#FF880018')
    expect(screen.getByRole('button', { name: 'Clear' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Translucent' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'Opaque' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('the opacity slider at 50 % gives alpha 80', () => {
    const onChange = vi.fn()
    render(<Held initial="#FF8800FF" onChange={onChange} />)
    const slider = screen.getByRole('slider', { name: 'Opacity' })
    expect(slider).toHaveAttribute('aria-valuenow', '100')
    fireEvent.keyDown(slider, { key: 'Home' })
    for (let step = 0; step < 5; step += 1) fireEvent.keyDown(slider, { key: 'PageUp' })
    expect(slider).toHaveAttribute('aria-valuenow', '50')
    expect(onChange).toHaveBeenLastCalledWith('#FF880080')
  })

  it('takes a typed hex, six digits as opaque', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(<Held initial="#FF8800FF" onChange={onChange} />)
    const hex = screen.getByRole('textbox', { name: 'Colour (hex)' })
    await user.clear(hex)
    await user.type(hex, '#1b6ca8')
    expect(onChange).toHaveBeenLastCalledWith('#1B6CA8FF')
    await user.type(hex, '80')
    expect(onChange).toHaveBeenLastCalledWith('#1B6CA880')
  })

  it('says a hex it cannot read is not a colour, and sends nothing', async () => {
    const { onChange, user } = setup('#FF8800FF')
    const hex = screen.getByRole('textbox', { name: 'Colour (hex)' })
    await user.clear(hex)
    await user.type(hex, '#12')
    expect(hex).toHaveAttribute('aria-invalid', 'false')
    await user.tab()
    expect(onChange).not.toHaveBeenCalled()
    expect(hex).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('alert')).toHaveTextContent('Use six or eight hex digits: #RRGGBB or #RRGGBBAA.')
  })

  it('drops the error once the hex reads, and waits for the next leave to say it again', async () => {
    const { user } = setup('#FF8800FF')
    const hex = screen.getByRole('textbox', { name: 'Colour (hex)' })
    await user.clear(hex)
    await user.type(hex, '#12')
    await user.tab()
    expect(screen.getByRole('alert')).toBeInTheDocument()
    await user.click(hex)
    await user.type(hex, '3456')
    expect(hex).toHaveAttribute('aria-invalid', 'false')
    expect(screen.queryByRole('alert')).toBeNull()
    await user.type(hex, '7')
    expect(hex).toHaveAttribute('aria-invalid', 'false')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('takes a hex with spaces around it, as typed or pasted', async () => {
    const { onChange, user } = setup('#FF8800FF')
    const hex = screen.getByRole('textbox', { name: 'Colour (hex)' })
    await user.clear(hex)
    await user.type(hex, ' #1b6ca8 ')
    expect(onChange).toHaveBeenLastCalledWith('#1B6CA8FF')
  })

  it('the native picker changes the colour and keeps the alpha', () => {
    const { onChange } = setup('#FF880080')
    const picker = screen.getByLabelText('Pick a colour')
    expect(picker).toHaveValue('#ff8800')
    fireEvent.change(picker, { target: { value: '#1b6ca8' } })
    expect(onChange).toHaveBeenLastCalledWith('#1B6CA880')
  })

  it('shows the inherited colour as a placeholder while the value is unset', async () => {
    const { onChange, user } = setup(null, '#3F8E43FF')
    const hex = screen.getByRole('textbox', { name: 'Colour (hex)' })
    expect(hex).toHaveValue('')
    expect(hex).toHaveAttribute('placeholder', '#3F8E43FF')
    expect(screen.getByTestId('rgba-swatch')).toHaveAttribute('data-colour', '#3F8E43FF')
    await user.click(screen.getByRole('button', { name: 'Translucent' }))
    expect(onChange).toHaveBeenLastCalledWith('#3F8E4380')
  })

  it('shows the colour it is in the swatch', () => {
    setup('#FFFFFF80')
    expect(screen.getByTestId('rgba-swatch')).toHaveAttribute('data-colour', '#FFFFFF80')
  })
})
