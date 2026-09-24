import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FontFamily, InstalledFamily } from '../../api/types'
import { setCatalogueOffline } from '../../mocks/handlers'
import { FontPicker } from './FontPicker'

const installed: FontFamily[] = [
  { family: 'Noto Sans', styles: ['Regular'] },
  { family: 'DejaVu Sans', styles: ['Book', 'Bold'] },
]

function setup(props: Partial<Parameters<typeof FontPicker>[0]> = {}) {
  const onPick = vi.fn()
  const onClose = vi.fn()
  const user = userEvent.setup()
  render(
    <FontPicker
      open
      family="Lobster Two"
      sampleText="Reagan"
      installed={installed}
      onClose={onClose}
      onPick={onPick}
      {...props}
    />,
  )
  return { onPick, onClose, user }
}

function rows() {
  return within(screen.getByTestId('font-rows')).queryAllByRole('button')
}

async function loaded() {
  await waitFor(() => expect(rows().length).toBeGreaterThan(0))
}

beforeEach(() => window.localStorage.clear())

describe('FontPicker', () => {
  it('lists the catalogue with each row drawn in its own family', async () => {
    setup()
    await loaded()

    const pacifico = screen.getByRole('button', { name: /Pacifico/ })
    const preview = within(pacifico).getByText('Reagan', { selector: 'span' })
    expect(preview).toHaveStyle({ fontFamily: '"Pacifico", sans-serif' })
  })

  it('previews the parameter’s current text by default and follows the sample box', async () => {
    const { user } = setup()
    await loaded()
    expect(within(screen.getByTestId('font-rows')).getAllByText('Reagan').length).toBeGreaterThan(0)

    const sample = screen.getByRole('textbox', { name: 'Sample text' })
    await user.clear(sample)
    await user.type(sample, 'Nova')

    await waitFor(() =>
      expect(within(screen.getByTestId('font-rows')).getAllByText('Nova').length).toBeGreaterThan(0),
    )
  })

  it('marks what fontconfig already resolves', async () => {
    setup()
    await loaded()
    expect(screen.getByRole('button', { name: /Noto Sans/ })).toHaveTextContent('installed')
    expect(screen.getByRole('button', { name: /Pacifico/ })).not.toHaveTextContent('installed')
  })

  it('searches with a type-ahead', async () => {
    const { user } = setup()
    await loaded()

    await user.type(screen.getByRole('searchbox', { name: 'Search fonts' }), 'paci')

    await waitFor(() => expect(rows()).toHaveLength(1), { timeout: 3000 })
    expect(rows()[0]).toHaveTextContent('Pacifico')
  })

  it('filters by category chip', async () => {
    const { user } = setup()
    await loaded()

    await user.click(screen.getByRole('button', { name: 'Script', pressed: false }))

    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(rows()[0]).toHaveTextContent('Pacifico')
  })

  it('installs the family it is given and reports the styles back', async () => {
    const { user, onPick, onClose } = setup()
    await loaded()

    await user.click(screen.getByRole('button', { name: /Pacifico/ }))

    await waitFor(() => expect(onPick).toHaveBeenCalledTimes(1))
    const installedFamily = onPick.mock.calls[0]![0] as InstalledFamily
    expect(installedFamily.family).toBe('Pacifico')
    expect(installedFamily.styles).toEqual(['Regular'])
    expect(onClose).toHaveBeenCalled()
  })

  it('reports a failed download on the widget instead of letting the render go blank', async () => {
    const { user, onPick } = setup()
    await loaded()

    await user.click(screen.getByRole('button', { name: /Playfair Display/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent('could not be downloaded')
    expect(onPick).not.toHaveBeenCalled()
  })

  it('puts a recently used family first when browsing', async () => {
    window.localStorage.setItem('scadbuddy.recent-fonts', JSON.stringify(['Pacifico']))
    setup()
    await loaded()
    expect(rows()[0]).toHaveTextContent('Pacifico')
  })

  it('falls back to the installed fonts when the catalogue is unreachable', async () => {
    setCatalogueOffline(true)
    setup()

    expect(await screen.findByRole('status')).toHaveTextContent('Google Fonts is unreachable')
    await waitFor(() => expect(rows()).toHaveLength(2))
    expect(rows().map((row) => row.textContent)).toEqual([
      expect.stringContaining('Noto Sans'),
      expect.stringContaining('DejaVu Sans'),
    ])
  })

  it('can still pick an installed font while offline', async () => {
    setCatalogueOffline(true)
    const { user, onPick } = setup()
    await waitFor(() => expect(rows()).toHaveLength(2))

    await user.click(screen.getByRole('button', { name: /Noto Sans/ }))

    await waitFor(() => expect(onPick).toHaveBeenCalled())
  })

  it('renders nothing while closed', () => {
    const onPick = vi.fn()
    const { container } = render(
      <FontPicker
        open={false}
        family=""
        sampleText=""
        installed={installed}
        onClose={vi.fn()}
        onPick={onPick}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})

describe('the widget it feeds', () => {
  it('is a controlled value, so the picked font lands on the parameter', async () => {
    function Harness() {
      const [value, setValue] = useState('Lobster Two:style=Bold')
      const [open, setOpen] = useState(true)
      return (
        <>
          <output data-testid="value">{value}</output>
          <FontPicker
            open={open}
            family="Lobster Two"
            sampleText="Reagan"
            installed={installed}
            onClose={() => setOpen(false)}
            onPick={(font) => setValue(`${font.family}:style=${(font.styles ?? [])[0] ?? ''}`)}
          />
        </>
      )
    }
    const user = userEvent.setup()
    render(<Harness />)
    await waitFor(() => expect(rows().length).toBeGreaterThan(0))

    await user.click(screen.getByRole('button', { name: /Pacifico/ }))

    await waitFor(() =>
      expect(screen.getByTestId('value')).toHaveTextContent('Pacifico:style=Regular'),
    )
  })
})
