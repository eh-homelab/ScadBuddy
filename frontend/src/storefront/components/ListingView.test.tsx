import { render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { CHARM, KEYCHAIN, TAG } from '../../mocks/features/storefrontPublic'
import type { ParamValue } from '../api/client'
import { ListingView } from './ListingView'

describe('ListingView', () => {
  it('shows each offered parameter, with the shop’s choice until the visitor sets one', async () => {
    const onChange = vi.fn()
    const onPick = vi.fn()
    render(<ListingView listing={KEYCHAIN} values={{}} onChange={onChange} onPick={onPick} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Name keychain' })).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('')
    await userEvent.type(screen.getByLabelText('Length (mm)'), '5')
    expect(onChange).toHaveBeenLastCalledWith('size', 5)
    // #6152: a colour is a picker of filaments; a pick sets the value and names the filament.
    await userEvent.click(screen.getByRole('button', { name: /Base colour/ }))
    await userEvent.click(screen.getByRole('radio', { name: 'Signal orange PLA' }))
    expect(onChange).toHaveBeenLastCalledWith('base_colour', '#E8532F')
    expect(onPick).toHaveBeenLastCalledWith('base_colour', 'f-orange')
  })

  it('shows one Material control for a one-material listing, and its colours only (#6152)', async () => {
    const onMaterial = vi.fn()
    const { rerender } = render(<ListingView listing={CHARM} values={{}} onChange={() => undefined} onMaterial={onMaterial} />)
    const material = screen.getByRole('radiogroup', { name: 'Material' })
    expect(within(material).getByRole('radio', { name: 'PLA' })).toHaveAttribute('aria-checked', 'true')
    await userEvent.click(within(material).getByRole('radio', { name: 'PETG' }))
    expect(onMaterial).toHaveBeenCalledWith('PETG')
    rerender(<ListingView listing={CHARM} values={{}} onChange={() => undefined} material="PETG" onMaterial={onMaterial} />)
    await userEvent.click(screen.getByRole('button', { name: /Body colour/ }))
    expect(screen.getByRole('radio', { name: 'Teal PETG' })).toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: /Ocean blue/ })).toBeNull()
  })

  it('shows the shop’s colour of the material on a one-material row with no pick, never “Choose a colour” (#6152)', () => {
    render(<ListingView listing={CHARM} values={{}} onChange={() => undefined} material="PETG" />)
    // The start colour, Ocean blue, is PLA only: the nearest PETG colour is Teal.
    expect(screen.getByRole('button', { name: /Body colour/ })).toHaveAccessibleName('Body colour Teal')
    expect(screen.queryByText('Choose a colour')).toBeNull()
  })

  it('offers a switch as Yes and No, never the words true and false (#6141)', async () => {
    const onChange = vi.fn()
    const listing = {
      ...KEYCHAIN,
      params: { ring: { kind: 'choice' as const, label: 'Keyring hole', group: '', colour: false, choices: [true, false] } },
    }
    render(<ListingView listing={listing} values={{}} onChange={onChange} />)
    expect(screen.queryByText('true')).toBeNull()
    expect(screen.queryByText('false')).toBeNull()
    await userEvent.click(screen.getByRole('radio', { name: 'No' }))
    expect(onChange).toHaveBeenLastCalledWith('ring', false)
    expect(screen.getByRole('radio', { name: 'Yes' })).toBeInTheDocument()
  })

  it('shows the design’s default text as the placeholder, or none (#6137)', () => {
    const listing = {
      ...KEYCHAIN,
      params: {
        name: { kind: 'text' as const, label: 'Name', group: '', colour: false, max_length: 20, placeholder: 'Stella' },
        motto: { kind: 'text' as const, label: 'Motto', group: '', colour: false, max_length: 20, placeholder: null },
      },
    }
    render(<ListingView listing={listing} values={{}} onChange={() => undefined} />)
    expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', 'Stella')
    expect(screen.getByLabelText('Motto')).not.toHaveAttribute('placeholder')
    expect(screen.queryByPlaceholderText("The shop's text")).toBeNull()
  })

  it('says a design with no colour left cannot be ordered', () => {
    render(<ListingView listing={TAG} values={{}} onChange={() => undefined} headingLevel={2} />)
    expect(screen.getByRole('heading', { level: 2, name: 'Luggage tag' })).toBeInTheDocument()
    expect(screen.getByText('This design cannot be ordered right now.')).toBeInTheDocument()
    expect(screen.getByText('No colour is available for this right now.')).toBeInTheDocument()
  })

  it('renders the shop’s text as text', () => {
    const hostile = { ...KEYCHAIN, title: '<b>x</b>', description: '<img src=x onerror=alert(1)>' }
    render(<ListingView listing={hostile} values={{}} onChange={() => undefined} />)
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(document.querySelector('img[src="x"], b')).toBeNull()
  })

  it('shows the shop’s default as picked until the visitor picks (#6144)', async () => {
    render(<ListingView listing={KEYCHAIN} values={{}} onChange={vi.fn()} />)
    // #6152: the colour row names the shop's colour; its grid shows it picked.
    expect(screen.getByRole('button', { name: /Base colour/ })).toHaveAccessibleName('Base colour Ocean blue')
    await userEvent.click(screen.getByRole('button', { name: /Base colour/ }))
    expect(screen.getByRole('radio', { name: 'Ocean blue PLA' })).toHaveAttribute('data-state', 'on')
    expect(screen.getByRole('radio', { name: 'Signal orange PLA' })).toHaveAttribute('data-state', 'off')
    expect(screen.getByLabelText('Length (mm)')).toHaveAttribute('placeholder', '60')
  })

  it('sends no change when the visitor taps the shop’s default, so it never says “Tap it again” (#6144)', async () => {
    const onChange = vi.fn()
    function Stateful() {
      const [values, setValues] = useState<Record<string, ParamValue>>({})
      return (
        <ListingView
          listing={KEYCHAIN}
          values={values}
          onChange={(name, value) => {
            onChange(name, value)
            setValues(({ [name]: _old, ...rest }) => (value === undefined ? rest : { ...rest, [name]: value }))
          }}
        />
      )
    }
    render(<Stateful />)
    const row = () => screen.getByRole('button', { name: /Base colour/ })
    const hint = () => document.getElementById(row().closest('[aria-describedby]')!.getAttribute('aria-describedby')!)
    expect(hint()).toHaveTextContent("The shop's choice, unless you pick one.")
    await userEvent.click(row())
    await userEvent.click(screen.getByRole('radio', { name: 'Ocean blue PLA' }))
    expect(onChange.mock.calls.every(([, value]) => value === undefined)).toBe(true)
    expect(row()).toHaveAccessibleName('Base colour Ocean blue')
    await userEvent.click(row())
    expect(screen.getByRole('radio', { name: 'Ocean blue PLA' })).toHaveAttribute('data-state', 'on')
    expect(hint()).toHaveTextContent("The shop's choice, unless you pick one.")
    expect(screen.queryByText(/Tap it again/)).toBeNull()
  })
})
