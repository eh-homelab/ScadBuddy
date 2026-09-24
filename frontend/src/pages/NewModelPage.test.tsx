import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { BROKEN_SOURCE, keychainSchema, keychainSource } from '../mocks/fixtures'
import { NewModelPage } from './NewModelPage'

// Monaco needs layout, workers and a canvas, none of which jsdom has; the real editor
// is exercised by the Playwright smoke test. Here it stands in as a textarea so the
// page's own behaviour — check, refuse, force — is what is under test.
vi.mock('../components/SourceEditor', () => ({
  SourceEditor: ({
    value,
    onChange,
    label,
    errors = [],
  }: {
    value: string
    onChange: (next: string) => void
    label: string
    errors?: { line?: number | null }[]
  }) => (
    <textarea
      aria-label={label}
      data-marked-lines={errors.map((error) => error.line ?? '').join(',')}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}))

function renderNew() {
  const user = userEvent.setup()
  const view = render(
    <MemoryRouter initialEntries={['/new']}>
      <Routes>
        <Route path="/new" element={<NewModelPage />} />
        <Route path="/m/:slug" element={<h1>Customizer</h1>} />
      </Routes>
    </MemoryRouter>,
  )
  return { user, ...view }
}

async function paste(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.click(screen.getByLabelText('OpenSCAD source'))
  await user.paste(text)
}

describe('NewModelPage', () => {
  it('will not save without a name and some source', async () => {
    const { user } = renderNew()
    const save = screen.getByRole('button', { name: 'Save and customize' })
    expect(save).toBeDisabled()

    await paste(user, keychainSource)
    expect(save).toBeDisabled()

    await user.type(screen.getByLabelText('Name'), 'Pasted Keychain')
    expect(save).toBeEnabled()
  })

  it('checks the source as it settles, without saving it', async () => {
    const { user } = renderNew()
    await paste(user, keychainSource)

    expect(await screen.findByText(/^Parses cleanly/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Customizer' })).not.toBeInTheDocument()
  })

  it('reports the derived parameter count alongside a clean parse', async () => {
    const { user } = renderNew()
    await paste(user, keychainSource)

    const derived = keychainSchema.parameters?.length ?? 0
    expect(
      await screen.findByText(`Parses cleanly — ${derived} parameters.`),
    ).toBeInTheDocument()
  })

  it('reports the failing line and hands it to the editor as a marker', async () => {
    const { user } = renderNew()
    await paste(user, BROKEN_SOURCE)

    const report = await screen.findByTestId('check-report')
    expect(report).toHaveTextContent('Line 2')
    expect(report).toHaveTextContent('Parser error: syntax error')
    expect(screen.getByLabelText('OpenSCAD source')).toHaveAttribute('data-marked-lines', '2')
  })

  it('drops a verdict the moment the source it judged changes', async () => {
    const { user } = renderNew()
    await paste(user, BROKEN_SOURCE)
    await screen.findByTestId('check-report')

    await user.type(screen.getByLabelText('OpenSCAD source'), ']')
    expect(screen.queryByTestId('check-report')).not.toBeInTheDocument()
  })

  it('saves a pasted model and opens its customizer', async () => {
    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), 'Pasted Keychain')
    await paste(user, keychainSource)

    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
  })

  it('refuses a save that does not parse until it is forced', async () => {
    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), 'Half Cube')
    await paste(user, BROKEN_SOURCE)

    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    expect(await screen.findByTestId('check-report')).toHaveTextContent('Line 2')
    expect(screen.queryByRole('heading', { name: 'Customizer' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Save anyway' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
  })

  it('reports a name that yields no slug', async () => {
    const { user } = renderNew()
    await user.type(screen.getByLabelText('Name'), '***')
    await paste(user, keychainSource)

    await user.click(screen.getByRole('button', { name: 'Save and customize' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('slug'))
  })
})
