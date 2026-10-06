import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { Param, ParamValue } from '../../api/types'
import { ParamWidget } from './ParamWidget'

const fonts = [
  { family: 'Liberation Sans', styles: ['Regular', 'Bold'] },
  { family: 'DejaVu Sans', styles: ['Book'] },
]

/** Widgets are controlled, so the harness holds the value the way the panel does. */
function Harness({
  param,
  initial,
  onChange,
}: {
  param: Param
  initial: ParamValue
  onChange: (next: ParamValue) => void
}) {
  const [value, setValue] = useState(initial)
  return (
    <ParamWidget
      param={param}
      value={value}
      slug="name-keychain"
      fonts={fonts}
      extruder={1}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

function setup(param: Param, value: ParamValue) {
  const onChange = vi.fn()
  const user = userEvent.setup()
  render(<Harness param={param} initial={value} onChange={onChange} />)
  return { onChange, user }
}

describe('slider', () => {
  const param: Param = {
    group: 'Main',
    name: 'text_size',
    type: 'slider',
    initial: 14,
    caption: 'Text size',
    min: 6,
    max: 28,
    step: 0.5,
  }

  it('renders a range and a number input that share min, max and step', () => {
    setup(param, 14)
    const range = screen.getByRole('slider', { name: 'Text size' })
    const number = screen.getByRole('spinbutton', { name: 'Text size value' })

    for (const input of [range, number]) {
      expect(input).toHaveAttribute('min', '6')
      expect(input).toHaveAttribute('max', '28')
      expect(input).toHaveAttribute('step', '0.5')
    }
    expect(range).toHaveValue('14')
  })

  it('reports numbers, not strings', async () => {
    const { onChange, user } = setup(param, 14)
    await user.clear(screen.getByRole('spinbutton', { name: 'Text size value' }))
    await user.type(screen.getByRole('spinbutton', { name: 'Text size value' }), '2')
    expect(onChange).toHaveBeenLastCalledWith(2)
  })

  it('shows the current value as a readout', () => {
    setup(param, 21.5)
    expect(screen.getByText('21.5')).toBeInTheDocument()
  })
})

describe('number and integer', () => {
  it('steps by 0.1 for a number', () => {
    setup({ group: 'Main', name: 'padding', type: 'number', initial: 6, caption: 'Margin' }, 6)
    expect(screen.getByRole('spinbutton', { name: 'Margin' })).toHaveAttribute('step', '0.1')
  })

  it('rounds an integer parameter', async () => {
    const { onChange, user } = setup(
      { group: 'Main', name: 'corner_radius', type: 'integer', initial: 4, caption: 'Corner radius' },
      4,
    )
    const input = screen.getByRole('spinbutton', { name: 'Corner radius' })
    expect(input).toHaveAttribute('step', '1')
    await user.clear(input)
    await user.type(input, '7.6')
    expect(onChange).toHaveBeenLastCalledWith(8)
  })
})

// #921: a value outside the declared range is flagged on the field, in its own words.
describe.each([
  {
    kind: 'number field',
    param: { group: 'Main', name: 'text_size', type: 'number', initial: 14, caption: 'Letter height', min: 8, max: 40 } as Param,
    label: 'Letter height',
  },
  {
    kind: 'slider box',
    param: {
      group: 'Main',
      name: 'text_size',
      type: 'slider',
      initial: 14,
      caption: 'Letter height',
      min: 8,
      max: 40,
      step: 1,
    } as Param,
    label: 'Letter height value',
  },
])('the $kind range (#921)', ({ param, label }) => {
  const field = () => screen.getByRole('spinbutton', { name: label })

  it('says an out-of-range value is out of range, with the label and the range', async () => {
    const { user } = setup(param, 14)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(field()).not.toHaveAttribute('aria-invalid')

    await user.clear(field())
    await user.type(field(), '500{Enter}')
    expect(field()).toHaveValue(500)
    expect(field()).toHaveAttribute('aria-invalid', 'true')
    expect(field()).toHaveAccessibleDescription('Letter height must be between 8 and 40.')
    expect(screen.getByRole('alert')).toHaveTextContent('Letter height must be between 8 and 40.')
  })

  it('clears the message once the value is back in range', async () => {
    const { user } = setup(param, 500)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    await user.clear(field())
    await user.type(field(), '40')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(field()).not.toHaveAttribute('aria-invalid')
  })
})

// #1323: a number box keeps what is typed as a draft and commits only a parseable number.
describe.each([
  {
    kind: 'number field',
    param: { group: 'Main', name: 'thickness', type: 'number', initial: 3, caption: 'Thickness' } as Param,
    label: 'Thickness',
  },
  {
    kind: 'slider box',
    param: {
      group: 'Main',
      name: 'thickness',
      type: 'slider',
      initial: 3,
      caption: 'Thickness',
      min: -10,
      max: 20,
      step: 0.5,
    } as Param,
    label: 'Thickness value',
  },
])('the $kind draft (#1323)', ({ param, label }) => {
  const field = () => screen.getByRole('spinbutton', { name: label })

  it('keeps a cleared field empty and commits nothing', async () => {
    const { onChange, user } = setup(param, 3)
    await user.clear(field())
    expect(field()).toHaveValue(null)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('commits a negative number typed into a cleared field', async () => {
    const { onChange, user } = setup(param, 3)
    await user.clear(field())
    await user.type(field(), '-')
    expect(onChange).not.toHaveBeenCalled()
    await user.type(field(), '3')
    expect(onChange.mock.calls).toEqual([[-3]])
    expect(field()).toHaveValue(-3)
  })

  it('commits an exponent only once it is complete', async () => {
    const { onChange, user } = setup(param, 3)
    await user.clear(field())
    await user.type(field(), '1e1')
    expect(onChange).toHaveBeenLastCalledWith(10)
    expect(onChange.mock.calls.flat()).not.toContain(0)
  })

  it('goes back to the last valid value when left empty', async () => {
    const { onChange, user } = setup(param, 3)
    await user.clear(field())
    await user.tab()
    expect(field()).toHaveValue(3)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('goes back to the last valid value on Enter', async () => {
    const { onChange, user } = setup(param, 3)
    await user.clear(field())
    await user.type(field(), '-{Enter}')
    expect(field()).toHaveValue(3)
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('string', () => {
  const param: Param = {
    group: 'Main',
    name: 'name',
    type: 'string',
    initial: 'Reagan',
    caption: 'Name on the tag',
    max_length: 20,
  }

  it('shows the count', () => {
    setup(param, 'Reagan')
    expect(screen.getByText('6/20')).toBeInTheDocument()
  })

  it('counts characters as OpenSCAD does, an emoji as one (#920)', () => {
    setup(param, 'Zoë 🦄 ß')
    expect(screen.getByText('7/20')).toBeInTheDocument()
  })

  it('stops at max_length characters, not UTF-16 units (#920)', async () => {
    const { onChange, user } = setup({ ...param, max_length: 3 }, '')
    const input = screen.getByRole('textbox', { name: 'Name on the tag' })
    expect(input).not.toHaveAttribute('maxlength')
    // Four UTF-16 units, two characters: under a maxlength of 3 only one would fit.
    await user.type(input, '🦄🦄')
    expect(onChange).toHaveBeenLastCalledWith('🦄🦄')
    await user.type(input, '🦄🦄')
    expect(onChange).toHaveBeenLastCalledWith('🦄🦄🦄')
    expect(input).toHaveValue('🦄🦄🦄')
  })

  it('refuses an edit past max_length wherever the caret is, keeping the name whole (#920)', async () => {
    const { onChange, user } = setup({ ...param, max_length: 6 }, 'Reagan')
    const input = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.click(input)
    await user.keyboard('{Home}X')
    expect(onChange).not.toHaveBeenCalled()
    expect(input).toHaveValue('Reagan')
    await user.paste('XY')
    expect(input).toHaveValue('Reagan')
  })

  it('reports each keystroke', async () => {
    const { onChange, user } = setup(param, '')
    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'Nova')
    expect(onChange).toHaveBeenCalledTimes(4)
    expect(onChange).toHaveBeenLastCalledWith('Nova')
  })
})

describe('boolean', () => {
  const param: Param = {
    group: 'Main',
    name: 'keyring_hole',
    type: 'boolean',
    initial: true,
    caption: 'Keyring hole',
  }

  it('is a switch reflecting the value', () => {
    setup(param, true)
    expect(screen.getByRole('switch', { name: 'Keyring hole' })).toBeChecked()
  })

  it('toggles', async () => {
    const { onChange, user } = setup(param, true)
    await user.click(screen.getByRole('switch', { name: 'Keyring hole' }))
    expect(onChange).toHaveBeenCalledWith(false)
  })
})

describe('select', () => {
  const param: Param = {
    group: 'Main',
    name: 'hole_side',
    type: 'select',
    initial: 'left',
    caption: 'Hole position',
    options: [
      { name: 'Left', value: 'left' },
      { name: 'Right', value: 'right' },
    ],
  }

  it('labels options by name and submits their value', async () => {
    const { onChange, user } = setup(param, 'left')
    const select = screen.getByRole('combobox', { name: 'Hole position' })
    expect(screen.getByRole('option', { name: 'Left' })).toBeInTheDocument()
    await user.selectOptions(select, 'right')
    expect(onChange).toHaveBeenCalledWith('right')
  })

  it('keeps numeric option values numeric', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(
      <ParamWidget
        slug="name-keychain"
        param={{
          group: 'Main',
          name: 'layers',
          type: 'select',
          initial: 1,
          caption: 'Layers',
          options: [
            { name: 'One', value: 1 },
            { name: 'Two', value: 2 },
          ],
        }}
        value={1}
        fonts={fonts}
        onChange={onChange}
      />,
    )
    await user.selectOptions(screen.getByRole('combobox', { name: 'Layers' }), '2')
    expect(onChange).toHaveBeenCalledWith(2)
  })
})

describe('color', () => {
  const param: Param = {
    group: 'Colours',
    name: 'body_color',
    type: 'color',
    initial: '#1B6CA8',
    caption: 'Plate',
  }

  it('shows the extruder it maps to', () => {
    setup(param, '#1B6CA8')
    expect(screen.getByText('extruder 1')).toBeInTheDocument()
  })

  it('normalises a short hex on blur', async () => {
    const { onChange, user } = setup(param, '#ABC')
    const hex = screen.getByRole('textbox', { name: 'Plate hex' })
    await user.click(hex)
    await user.tab()
    expect(onChange).toHaveBeenLastCalledWith('#AABBCC')
  })

  it('pairs a colour picker with the hex field', () => {
    const { container } = render(
      <ParamWidget
        slug="name-keychain"
        param={param}
        value="#1B6CA8"
        fonts={fonts}
        extruder={2}
        onChange={vi.fn()}
      />,
    )
    const picker = container.querySelector('input[type="color"]')
    expect(picker).toHaveValue('#1b6ca8')
    expect(screen.getAllByDisplayValue('#1B6CA8')).not.toHaveLength(0)
  })
})

describe('font', () => {
  const param: Param = {
    group: 'Main',
    name: 'font',
    type: 'font',
    initial: 'Liberation Sans:style=Bold',
    caption: 'Typeface',
  }

  it('offers every installed family and style for completion', () => {
    setup(param, 'Liberation Sans:style=Bold')
    const options = screen.getByTestId('font-options').querySelectorAll('option')
    expect([...options].map((option) => option.getAttribute('value'))).toEqual([
      'Liberation Sans:style=Regular',
      'Liberation Sans:style=Bold',
      'DejaVu Sans:style=Book',
    ])
  })

  it('accepts a font the server did not list', async () => {
    const { onChange, user } = setup(param, '')
    await user.type(screen.getByRole('combobox', { name: 'Typeface' }), 'X')
    expect(onChange).toHaveBeenLastCalledWith('X')
  })

  it('draws the field in the family it names', () => {
    setup(param, 'Liberation Sans:style=Bold')
    expect(screen.getByRole('combobox', { name: 'Typeface' })).toHaveStyle({
      fontFamily: '"Liberation Sans", sans-serif',
    })
  })

  it('offers the installed styles of the current family and emits the OpenSCAD form', async () => {
    const { onChange, user } = setup(param, 'Liberation Sans:style=Bold')
    const styles = screen.getByRole('combobox', { name: 'Typeface style' })

    await user.selectOptions(styles, 'Regular')

    expect(onChange).toHaveBeenLastCalledWith('Liberation Sans:style=Regular')
  })

  it('has no style dropdown for a family with a single face', () => {
    setup(param, 'DejaVu Sans:style=Book')
    expect(screen.queryByRole('combobox', { name: 'Typeface style' })).not.toBeInTheDocument()
  })

  it('opens the picker on Browse and installs what is chosen', async () => {
    const { onChange, user } = setup(param, 'Liberation Sans:style=Bold')

    await user.click(screen.getByRole('button', { name: 'Browse' }))
    const dialog = await screen.findByRole('dialog', { name: 'Choose a font' })
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: /Pacifico/ })).toBeInTheDocument(),
    )

    await user.click(within(dialog).getByRole('button', { name: /Pacifico/ }))

    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith('Pacifico:style=Regular'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('seeds the picker preview with the model\u2019s own text', async () => {
    const user = userEvent.setup()
    render(
      <ParamWidget
        slug="name-keychain"
        param={param}
        value="Liberation Sans:style=Bold"
        fonts={fonts}
        sampleText="Reagan"
        onChange={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Browse' }))

    expect(await screen.findByRole('textbox', { name: 'Sample text' })).toHaveValue('Reagan')
  })
})
