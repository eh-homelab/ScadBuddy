import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { listCatalog } from '../../api/filament'
import { server } from '../../mocks/server'
import { RgbaColourField } from './RgbaColourField'

function setup(value: string | null, inherited: string | null = null) {
  const onChange = vi.fn()
  const user = userEvent.setup()
  const view = render(<RgbaColourField value={value} onChange={onChange} inherited={inherited} />)
  return { onChange, user, ...view }
}

/** The field as a form holds it: each change comes back as its value. */
function Held({ initial, onChange, catalog = false }: { initial: string | null; onChange: (rgba: string) => void; catalog?: boolean }) {
  const [value, setValue] = useState(initial)
  return (
    <RgbaColourField
      value={value}
      catalog={catalog}
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

describe('the catalog pick (#6912)', () => {
  function pick(value: string | null = '#FF8800FF') {
    const onChange = vi.fn()
    const onExtraColours = vi.fn()
    const user = userEvent.setup()
    render(<RgbaColourField value={value} onChange={onChange} catalog onExtraColours={onExtraColours} />)
    return { onChange, onExtraColours, user }
  }

  async function search(user: ReturnType<typeof userEvent.setup>, text: string) {
    await user.click(screen.getByRole('button', { name: 'From the catalog' }))
    await user.type(screen.getByRole('combobox', { name: 'Search the colour catalog' }), text)
  }

  it('is offered only where the form asks for it', () => {
    setup('#FF8800FF')
    expect(screen.queryByRole('button', { name: 'From the catalog' })).toBeNull()
  })

  it('lists nothing until a word is typed', async () => {
    const { user } = pick()
    await user.click(screen.getByRole('button', { name: 'From the catalog' }))
    expect(await screen.findByText('Type a colour name, maker, material or hex.')).toBeInTheDocument()
    expect(screen.queryByRole('option')).toBeNull()
  })

  it('shows each match with its swatch, name, maker and material, and fills the RGBA from a pick', async () => {
    const { onChange, onExtraColours, user } = pick()
    await search(user, 'jade')
    const option = await screen.findByRole('option', { name: /Jade White/ })
    expect(option).toHaveTextContent('Bambu Lab')
    expect(option).toHaveTextContent('PLA Basic')
    expect(within(option).getByTestId('rgba-swatch')).toHaveAttribute('data-colour', '#FFFFFFFF')
    await user.click(option)
    expect(onChange).toHaveBeenLastCalledWith('#FFFFFFFF')
    expect(onExtraColours).toHaveBeenLastCalledWith([])
    expect(screen.queryByRole('combobox', { name: 'Search the colour catalog' })).toBeNull()
  })

  it('keeps a translucent colour’s alpha, and names the material from the materials catalog when the maker gives none', async () => {
    const { onChange, user } = pick()
    await search(user, 'orange')
    const option = await screen.findByRole('option', { name: /Translucent Orange/ })
    expect(option).toHaveTextContent('PETG')
    await user.click(option)
    expect(onChange).toHaveBeenLastCalledWith('#FF6A1380')
  })

  it('finds a colour by maker and hex, every word matching', async () => {
    const { user } = pick()
    await search(user, 'bambu #f5547c')
    expect(await screen.findByRole('option', { name: /Hot Pink/ })).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(1)
  })

  it('fills the extra colours of a multi-colour pick', async () => {
    const { onChange, onExtraColours, user } = pick()
    await search(user, 'galaxy')
    const option = await screen.findByRole('option', { name: /Galaxy Duo/ })
    expect(within(option).getByTestId('rgba-extra')).toHaveAttribute('data-colour', '#2E8B57FF')
    await user.click(option)
    expect(onChange).toHaveBeenLastCalledWith('#1B3A8CFF')
    expect(onExtraColours).toHaveBeenLastCalledWith(['#2E8B57FF'])
  })

  it('never offers an archived row', async () => {
    expect((await listCatalog('colours', { includeArchived: true, q: 'retired' })).map((row) => row.name)).toEqual(['Retired Grey'])
    const { user } = pick()
    await search(user, 'retired')
    expect(await screen.findByText('No catalog colour matches “retired”.')).toBeInTheDocument()
    expect(screen.queryByRole('option')).toBeNull()
  })

  it('shows a pick in the hex field, and any typed RGBA still overrides it', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(<Held initial="#FF8800FF" onChange={onChange} catalog />)
    await search(user, 'jade')
    await user.click(await screen.findByRole('option', { name: /Jade White/ }))
    const hex = screen.getByRole('textbox', { name: 'Colour (hex)' })
    expect(hex).toHaveValue('#FFFFFFFF')
    await user.clear(hex)
    await user.type(hex, '#123456AA')
    expect(onChange).toHaveBeenLastCalledWith('#123456AA')
  })

  it('reads the materials once, however often the pick opens', async () => {
    let reads = 0
    server.use(
      http.get('/api/v1/filament/catalog/materials', () => {
        reads += 1
        return undefined
      }),
    )
    const { user } = pick()
    await search(user, 'orange')
    expect(await screen.findByRole('option', { name: /Translucent Orange.*PETG/ })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('combobox', { name: 'Search the colour catalog' })).toBeNull()
    await search(user, 'orange')
    expect(await screen.findByRole('option', { name: /Translucent Orange.*PETG/ })).toBeInTheDocument()
    expect(reads).toBe(1)
  })

  it('says why a search failed, in an alert', async () => {
    server.use(
      http.get('/api/v1/filament/catalog/colours', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Service Unavailable', status: 503, detail: 'the database did not answer' }, { status: 503, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    const { onChange, user } = pick()
    await search(user, 'jade')
    expect(await screen.findByRole('alert')).toHaveTextContent('the database did not answer')
    expect(screen.queryByRole('option')).toBeNull()
    expect(onChange).not.toHaveBeenCalled()
  })
})
