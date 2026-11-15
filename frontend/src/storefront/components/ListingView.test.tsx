import { render, screen } from '@testing-library/react'
import { useState } from 'react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { CHARM, KEYCHAIN, TAG } from '../../mocks/features/storefrontPublic'
import type { Listing, ParamValue } from '../api/client'
import { ListingView } from './ListingView'

describe('ListingView', () => {
  it('shows each offered parameter, with the shop’s choice until the visitor sets one', async () => {
    const onChange = vi.fn()
    render(<ListingView listing={KEYCHAIN} values={{}} onChange={onChange} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Name keychain' })).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('')
    await userEvent.type(screen.getByLabelText('Length (mm)'), '5')
    expect(onChange).toHaveBeenLastCalledWith('size', 5)
    // #6152: a colour is a picker of filaments; #6258: a pick is the colour in the values.
    await userEvent.click(screen.getByRole('button', { name: /Base colour/ }))
    await userEvent.click(screen.getByRole('radio', { name: 'Signal orange' }))
    expect(onChange).toHaveBeenLastCalledWith('base_colour', '#E8532F')
  })

  it('shows no Material control: one material is always on, and the shop resolves it (#6258)', () => {
    render(<ListingView listing={CHARM} values={{}} onChange={() => undefined} />)
    expect(screen.queryByRole('radiogroup', { name: 'Material' })).toBeNull()
    // The shop's colour shows on a row with no colour set, never "Choose a colour".
    expect(screen.getByRole('button', { name: /Body colour/ })).toHaveAccessibleName('Body colour Ocean blue')
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

  it('says a design no material can print cannot be ordered (#6302 review)', () => {
    // The shop's locked colours share no material with any choice: it answers `available`
    // false, though the design offers no colour of its own.
    const { size } = KEYCHAIN.params
    if (!size) throw new Error('KEYCHAIN has no size')
    const listing = { ...KEYCHAIN, params: { size }, available: false }
    render(<ListingView listing={listing} values={{}} onChange={() => undefined} />)
    expect(screen.getByText('This design cannot be ordered right now.')).toBeInTheDocument()
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
    expect(screen.getByRole('radio', { name: 'Ocean blue' })).toHaveAttribute('data-state', 'on')
    expect(screen.getByRole('radio', { name: 'Signal orange' })).toHaveAttribute('data-state', 'off')
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
    await userEvent.click(screen.getByRole('radio', { name: 'Ocean blue' }))
    expect(onChange.mock.calls.every(([, value]) => value === undefined)).toBe(true)
    expect(row()).toHaveAccessibleName('Base colour Ocean blue')
    await userEvent.click(row())
    expect(screen.getByRole('radio', { name: 'Ocean blue' })).toHaveAttribute('data-state', 'on')
    expect(hint()).toHaveTextContent("The shop's choice, unless you pick one.")
    expect(screen.queryByText(/Tap it again/)).toBeNull()
  })
})

/**
 * #6258: CHARM with a third part, the loop, whose default black both materials offer. Blue
 * and orange are PLA only, teal and rust PETG only.
 */
const LOOPED: Listing = {
  ...CHARM,
  params: {
    ...CHARM.params,
    loop_colour: { kind: 'choice', label: 'Loop colour', group: '', colour: true, choices: ['#1B6CA8', '#1C8C8C', '#101010'], default: '#101010' },
  },
  colours: [
    ...CHARM.colours,
    { id: 'k1', colour: '#101010', colour_name: 'Black', material: 'PLA' },
    { id: 'k2', colour: '#101010', colour_name: 'Black', material: 'PETG' },
  ],
}

/** ListingView with the visitor's values kept, as the page keeps them. */
function Kept({ listing, onChange = () => undefined }: { listing: Listing; onChange?: (name: string, value: ParamValue | undefined) => void }) {
  const [values, setValues] = useState<Record<string, ParamValue>>({})
  return (
    <ListingView
      listing={listing}
      values={values}
      onChange={(name, value) => {
        onChange(name, value)
        setValues(({ [name]: _old, ...rest }) => (value === undefined ? rest : { ...rest, [name]: value }))
      }}
    />
  )
}

describe('ListingView colour cascade (#6258)', () => {
  it('moves an untouched part to its closest PETG colour at once when a PETG-only colour is picked, and keeps a default PETG offers', async () => {
    const onChange = vi.fn()
    render(<Kept listing={LOOPED} onChange={onChange} />)
    expect(screen.getByRole('button', { name: /Rim colour/ })).toHaveAccessibleName('Rim colour Ocean blue')
    await userEvent.click(screen.getByRole('button', { name: /Body colour/ }))
    await userEvent.click(screen.getByRole('radio', { name: 'Rust' }))
    expect(screen.getByRole('button', { name: /Rim colour/ })).toHaveAccessibleName('Rim colour Teal')
    expect(screen.getByRole('button', { name: /Loop colour/ })).toHaveAccessibleName('Loop colour Black')
    // The moved part is still the shop's choice: the page sends only the visitor's pick.
    expect(onChange.mock.calls).toEqual([['body_colour', '#C0502A']])
  })

  it('never moves a colour the visitor set', async () => {
    render(<Kept listing={LOOPED} />)
    await userEvent.click(screen.getByRole('button', { name: /Rim colour/ }))
    await userEvent.click(screen.getByRole('radio', { name: 'Signal orange' }))
    await userEvent.click(screen.getByRole('button', { name: /Body colour/ }))
    await userEvent.click(screen.getByRole('radio', { name: 'Rust' }))
    // Orange and rust share no material: the shop answers 422, and the page says so.
    expect(screen.getByRole('button', { name: /Rim colour/ })).toHaveAccessibleName('Rim colour Signal orange')
    expect(screen.getByRole('button', { name: /Body colour/ })).toHaveAccessibleName('Body colour Rust')
  })

  it('shows the colours a render start settled over its own prediction', () => {
    render(
      <ListingView
        listing={LOOPED}
        values={{ body_colour: '#C0502A' }}
        settled={{ body_colour: '#C0502A', rim_colour: '#C0502A', loop_colour: '#101010' }}
        onChange={() => undefined}
      />,
    )
    expect(screen.getByRole('button', { name: /Rim colour/ })).toHaveAccessibleName('Rim colour Rust')
  })
})
