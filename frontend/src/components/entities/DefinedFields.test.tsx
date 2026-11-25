import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { PropertyValue } from './api'
import { DefinedFields } from './DefinedFields'
import { propertiesPatch, type FieldErrors } from './definedProperties'

const SCHEMA = {
  type: 'object',
  properties: {
    td: { type: 'number', title: 'Transmission distance' },
    mpn: { type: 'string' },
    finish: { type: 'string', enum: ['matte', 'glossy'] },
    food_safe: { type: 'boolean' },
    gtin: { type: 'array', items: { type: 'string' } },
    asin: { type: 'string' },
  },
  required: ['mpn', 'asin'],
}

type Values = Record<string, PropertyValue | undefined>

function Harness({ start = {}, errors }: { start?: Values; errors?: FieldErrors }) {
  const [values, setValues] = useState<Values>(start)
  return (
    <>
      <DefinedFields schema={SCHEMA} values={values} onChange={setValues} errors={errors} />
      <output aria-label="values">{JSON.stringify(values)}</output>
    </>
  )
}

const values = () => JSON.parse(screen.getByRole('status', { name: 'values' }).textContent ?? '{}') as Values

describe('DefinedFields (#7703)', () => {
  it('renders required first then by key', () => {
    render(<Harness />)
    const group = screen.getByRole('group', { name: 'Defined properties' })
    const labels = within(group)
      .getAllByTestId('defined-field')
      .map((field) => field.dataset.key)
    expect(labels).toEqual(['asin', 'mpn', 'finish', 'food_safe', 'gtin', 'td'])
    expect(screen.getByLabelText('mpn')).toBeRequired()
    expect(screen.getByLabelText('Transmission distance')).not.toBeRequired()
  })

  it('edits each type of key', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.type(screen.getByLabelText('mpn'), 'A00-R1')
    await user.type(screen.getByLabelText('Transmission distance'), '1.5')
    await user.click(screen.getByRole('checkbox', { name: 'food_safe' }))
    await user.type(screen.getByLabelText('gtin item 1'), '0123')
    await user.click(screen.getByRole('button', { name: 'Add gtin item' }))
    await user.type(screen.getByLabelText('gtin item 2'), '0456')
    expect(values()).toMatchObject({ mpn: 'A00-R1', td: 1.5, food_safe: true, gtin: ['0123', '0456'] })
  })

  it('marks each missing and ill-fitting field', () => {
    render(<Harness errors={{ missing: ['mpn'], invalid: [{ key: 'td', reason: 'must be at least 0' }] }} />)
    expect(screen.getByLabelText('mpn')).toHaveAccessibleDescription('Required')
    expect(screen.getByLabelText('mpn')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('Transmission distance')).toHaveAccessibleDescription('must be at least 0')
  })

  it('an emptied optional field removes its key, an untouched one is left out', () => {
    const before = { mpn: 'A', td: 1, gtin: ['x'], asin: 'B' }
    expect(propertiesPatch(SCHEMA, before, { mpn: 'A', td: undefined, gtin: [''], asin: 'C' })).toEqual({ td: null, gtin: null, asin: 'C' })
    expect(propertiesPatch(SCHEMA, {}, { mpn: '  ', food_safe: false })).toEqual({ food_safe: false })
    expect(propertiesPatch(SCHEMA, { mpn: 'A' }, { mpn: 'A' })).toEqual({})
  })
})
