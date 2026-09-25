import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { BROKEN_SOURCE, keychainSource } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { EditSourcePage } from './EditSourcePage'

vi.mock('../components/SourceEditor', () => ({
  SourceEditor: ({
    value,
    onChange,
    label,
  }: {
    value: string
    onChange: (next: string) => void
    label: string
  }) => (
    <textarea
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}))

function renderEdit(slug = 'name-keychain') {
  const user = userEvent.setup()
  const view = render(
    <MemoryRouter initialEntries={[`/m/${slug}/source`]}>
      <Routes>
        <Route path="/m/:slug/source" element={<EditSourcePage />} />
        <Route path="/m/:slug" element={<h1>Customizer</h1>} />
      </Routes>
    </MemoryRouter>,
  )
  return { user, ...view }
}

describe('EditSourcePage', () => {
  it('opens prefilled with the model source', async () => {
    renderEdit()
    expect(await screen.findByLabelText('OpenSCAD source')).toHaveValue(keychainSource)
  })

  it('replaces the source and returns to the customizer', async () => {
    const replace = vi.spyOn(api, 'replaceSource')
    const { user } = renderEdit()
    const editor = await screen.findByLabelText('OpenSCAD source')

    await user.clear(editor)
    await user.click(editor)
    await user.paste('cube([10, 10, 10]);\n')
    await user.click(screen.getByRole('button', { name: 'Save source' }))

    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
    expect(replace).toHaveBeenCalledWith('name-keychain', 'cube([10, 10, 10]);\n', false)
    replace.mockRestore()
  })

  it('refuses a replacement that does not parse until it is forced', async () => {
    const { user } = renderEdit()
    const editor = await screen.findByLabelText('OpenSCAD source')

    await user.clear(editor)
    await user.click(editor)
    await user.paste(BROKEN_SOURCE)
    await user.click(screen.getByRole('button', { name: 'Save source' }))

    expect(await screen.findByTestId('check-report')).toHaveTextContent('Line 2')
    expect(screen.queryByRole('heading', { name: 'Customizer' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Save anyway' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
    expect(await api.getSource('name-keychain')).toBe(BROKEN_SOURCE)
  })

  it('says so when the model has no source to edit', async () => {
    server.use(
      http.get('/api/v1/models/:slug/source', () =>
        HttpResponse.json({ title: 'Not Found', status: 404 }, { status: 404 }),
      ),
    )
    renderEdit('gone')
    expect(await screen.findByRole('heading', { name: 'That source is not here' })).toBeInTheDocument()
  })

  it('checks against the model, so its sibling includes resolve', async () => {
    const check = vi.spyOn(api, 'checkSource')
    const { user } = renderEdit()
    const editor = await screen.findByLabelText('OpenSCAD source')

    await user.clear(editor)
    await user.click(editor)
    await user.paste('include <helper.scad>\ncube(1);\n')

    // The first check is for the source as it loaded; this waits for the edited one.
    await waitFor(() => {
      const last = check.mock.calls.at(-1)
      expect(last?.[0]).toContain('include <helper.scad>')
      expect(last?.[1]).toBe('name-keychain')
    })
    check.mockRestore()
  })
})
