import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { BROKEN_SOURCE, keychainSource } from '../mocks/fixtures'
import { NewModelPage } from './NewModelPage'

// CodeMirror owns a contenteditable, which jsdom cannot lay out; the real editor is
// exercised by the Playwright smoke test. Here it stands in as a textarea so the
// page's own behaviour — check, refuse, force — is what is under test.
vi.mock('../components/ScadEditor', () => ({
  ScadEditor: ({
    value,
    onChange,
    label,
    errorLines = [],
  }: {
    value: string
    onChange: (next: string) => void
    label: string
    errorLines?: number[]
  }) => (
    <textarea
      aria-label={label}
      data-error-lines={errorLines.join(',')}
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

  it('checks the source without saving it', async () => {
    const { user } = renderNew()
    await paste(user, keychainSource)

    await user.click(screen.getByRole('button', { name: 'Check' }))
    expect(await screen.findByText('Parses cleanly.')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Customizer' })).not.toBeInTheDocument()
  })

  it('reports the failing line and drops the verdict once the source changes', async () => {
    const { user } = renderNew()
    await paste(user, BROKEN_SOURCE)

    await user.click(screen.getByRole('button', { name: 'Check' }))
    const report = await screen.findByTestId('check-report')
    expect(report).toHaveTextContent('Line 2')
    expect(report).toHaveTextContent('Parser error: syntax error')
    expect(screen.getByLabelText('OpenSCAD source')).toHaveAttribute('data-error-lines', '2')

    // Editing invalidates the verdict rather than leaving a stale one on screen.
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
