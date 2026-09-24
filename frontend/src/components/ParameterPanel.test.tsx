import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema, ParamValue } from '../api/types'
import { fonts, keychainSchema } from '../mocks/fixtures'
import { defaultValues, type ParamValues } from '../lib/params'
import { ParameterPanel } from './ParameterPanel'

function Harness({ schema = keychainSchema }: { schema?: CustomizerSchema }) {
  const [values, setValues] = useState<ParamValues>(() => defaultValues(schema))
  return (
    <ParameterPanel
      schema={schema}
      values={values}
      fonts={fonts}
      onChange={(name: string, value: ParamValue) =>
        setValues((current) => ({ ...current, [name]: value }))
      }
      onReset={() => setValues(defaultValues(schema))}
    />
  )
}

describe('ParameterPanel', () => {
  it('shows one tab per group with the first one selected', () => {
    render(<Harness />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((tab) => tab.textContent)).toEqual(['Text', 'Plate', 'Colours'])
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('switches groups', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    expect(screen.queryByRole('switch', { name: 'Keyring hole' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Plate' }))
    expect(screen.getByRole('switch', { name: 'Keyring hole' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Plate' })).toHaveAttribute('aria-selected', 'true')
  })

  it('pins a Global group above every tab', async () => {
    const user = userEvent.setup()
    const schema: CustomizerSchema = {
      title: 'With globals',
      source_sha256: keychainSchema.source_sha256,
      groups: ['Global', ...(keychainSchema.groups ?? [])],
      parameters: [
        { group: 'Global', name: 'scale', type: 'number', initial: 1, caption: 'Scale' },
        ...(keychainSchema.parameters ?? []),
      ],
    }
    render(<Harness schema={schema} />)

    expect(screen.queryByRole('tab', { name: 'Global' })).not.toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: 'Scale' })).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Colours' }))
    expect(screen.getByRole('spinbutton', { name: 'Scale' })).toBeInTheDocument()
  })

  it('counts changes and resets back to the defaults', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const reset = screen.getByRole('button', { name: 'Reset to defaults' })
    expect(reset).toBeDisabled()
    expect(screen.getByText('Defaults')).toBeInTheDocument()

    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Nova')
    expect(screen.getByText('1 changed from defaults')).toBeInTheDocument()
    expect(reset).toBeEnabled()

    await user.click(reset)
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Reagan')
    expect(screen.getByText('Defaults')).toBeInTheDocument()
  })

  it('numbers colour parameters in extruder order', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByRole('tab', { name: 'Colours' }))
    const panel = screen.getByRole('tabpanel', { name: 'Colours' })
    expect(within(panel).getByText('extruder 1')).toBeInTheDocument()
    expect(within(panel).getByText('extruder 2')).toBeInTheDocument()
  })
})
