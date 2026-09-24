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

describe('string', () => {
  const param: Param = {
    group: 'Main',
    name: 'name',
    type: 'string',
    initial: 'Reagan',
    caption: 'Name on the tag',
    max_length: 20,
  }

  it('enforces maxLength and shows the count', () => {
    setup(param, 'Reagan')
    const input = screen.getByRole('textbox', { name: 'Name on the tag' })
    expect(input).toHaveAttribute('maxlength', '20')
    expect(screen.getByText('6/20')).toBeInTheDocument()
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
