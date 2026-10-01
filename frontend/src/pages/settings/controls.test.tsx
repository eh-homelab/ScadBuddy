import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { BytesInput } from './controls'
import { bestUnit, hasKey, inUnit } from './fields'

// #322 review: a byte limit must never change unless its number is edited.

function Harness({ initial }: { initial: string }) {
  const [value, setValue] = useState(initial)
  return (
    <>
      <BytesInput id="limit" value={value} onChange={setValue} />
      <output data-testid="bytes">{value}</output>
    </>
  )
}

describe('BytesInput', () => {
  it('converts the shown number when only the unit changes', async () => {
    const user = userEvent.setup()
    render(<Harness initial="2000000000" />)
    const number = screen.getByRole('spinbutton')
    expect(number).toHaveValue(2)
    await user.selectOptions(screen.getByLabelText('Unit for limit'), 'MB')
    expect(number).toHaveValue(2000)
    expect(screen.getByTestId('bytes')).toHaveTextContent(/^2000000000$/)

    await user.selectOptions(screen.getByLabelText('Unit for limit'), 'MiB')
    expect(screen.getByLabelText('Unit for limit')).toHaveValue('MiB')
    expect(screen.getByTestId('bytes')).toHaveTextContent(/^2000000000$/)
  })

  it('reinterprets under the current unit when the number is edited', async () => {
    const user = userEvent.setup()
    render(<Harness initial="2000000000" />)
    const number = screen.getByRole('spinbutton')
    await user.clear(number)
    await user.type(number, '3')
    expect(screen.getByTestId('bytes')).toHaveTextContent(/^3000000000$/)
  })

  it('shows a value round in no unit exactly, in bytes', () => {
    render(<Harness initial="123456789" />)
    expect(screen.getByRole('spinbutton')).toHaveValue(123456789)
    expect(screen.getByLabelText('Unit for limit')).toHaveValue('B')
  })
})

describe('byte units', () => {
  it('round-trip a value that is not a whole number of any larger unit', () => {
    const unit = bestUnit(123456789)
    expect(unit).toBe('B')
    expect(inUnit(123456789, unit)).toBe('123456789')
  })

  it('still show round values in the unit that holds them', () => {
    expect(bestUnit(1024 ** 3)).toBe('GiB')
    expect(bestUnit(1_000_000_000)).toBe('GB')
    expect(bestUnit(0)).toBe('MB')
  })
})

describe('hasKey', () => {
  it('names the flag each secret is reported under, the render key included (#855)', () => {
    expect(hasKey('bambuddy_api_key')).toBe('has_api_key')
    expect(hasKey('bambuddy_render_api_key')).toBe('has_render_api_key')
    expect(hasKey('google_fonts_api_key')).toBe('has_google_fonts_api_key')
  })
})
