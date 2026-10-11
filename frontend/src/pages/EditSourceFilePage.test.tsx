import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { BUILTIN_SLUG, keychainSource } from '../mocks/fixtures'
import { EditSourceFilePage } from './EditSourceFilePage'
import { EditSourcePage } from './EditSourcePage'

const HELPER = '// Shared by the keychain\nmodule rounded_plate(size, r) {\n  offset(r) square(size);\n}\n'

vi.mock('../components/SourceEditor', () => ({
  SourceEditor: ({
    value,
    onChange,
    label,
    readOnly,
  }: {
    value: string
    onChange: (next: string) => void
    label: string
    readOnly?: boolean
  }) => (
    <textarea aria-label={label} readOnly={readOnly} value={value} onChange={(event) => onChange(event.target.value)} />
  ),
}))

function renderAt(path: string) {
  const user = userEvent.setup()
  const view = render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/m/:slug/source" element={<EditSourcePage />} />
        <Route path="/m/:slug/source/:file" element={<EditSourceFilePage />} />
        <Route path="/m/:slug" element={<h1>Customizer</h1>} />
      </Routes>
    </MemoryRouter>,
  )
  return { user, ...view }
}

const files = async () => within(await screen.findByRole('navigation', { name: 'Source files' }))

async function retype(user: ReturnType<typeof userEvent.setup>, text: string) {
  const editor = await screen.findByLabelText('OpenSCAD source')
  await user.clear(editor)
  await user.click(editor)
  await user.paste(text)
}

async function saveFile(user: ReturnType<typeof userEvent.setup>) {
  const save = screen.getByRole('button', { name: 'Save file' })
  await waitFor(() => expect(save).toBeEnabled())
  await user.click(save)
}

describe('EditSourceFilePage (#1290)', () => {
  it("lists the model's files above Edit source, and opens another one in its own editor", async () => {
    const { user } = renderAt('/m/name-keychain/source')
    const list = await files()
    expect(list.getAllByRole('link').map((link) => link.textContent)).toEqual(['model.scad', 'helper.scad'])
    expect(list.getByRole('link', { name: 'model.scad' })).toHaveAttribute('aria-current', 'page')
    expect(await screen.findByLabelText('OpenSCAD source')).toHaveValue(keychainSource)

    await user.click(list.getByRole('link', { name: 'helper.scad' }))
    expect(await screen.findByRole('heading', { name: 'helper.scad' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue(HELPER))
    expect((await files()).getByRole('link', { name: 'helper.scad' })).toHaveAttribute('aria-current', 'page')
  })

  it('saves the file as one revision against the version it was read at, and stays on it', async () => {
    const { version } = await api.replaceSource('name-keychain', keychainSource)
    expect(version).toBeTruthy()
    const write = vi.spyOn(api, 'writeSourceFile')
    const { user } = renderAt('/m/name-keychain/source/helper.scad')
    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue(HELPER))

    await retype(user, 'module plate() { cube(2); }\n')
    await saveFile(user)

    await waitFor(() =>
      expect(write).toHaveBeenCalledWith('name-keychain', 'helper.scad', 'module plate() { cube(2); }\n', version ?? undefined),
    )
    expect(await api.getDefinitionFile('name-keychain', { path: 'helper.scad' })).toBe('module plate() { cube(2); }\n')
    expect(screen.getByRole('heading', { name: 'helper.scad' })).toBeInTheDocument()
    write.mockRestore()
  })

  it('offers to reload or save over a model changed since the file was read', async () => {
    // A model with a revision, so the save carries a base.
    await api.replaceSource('name-keychain', keychainSource)
    const { user } = renderAt('/m/name-keychain/source/helper.scad')
    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue(HELPER))
    // Another writer (a tab, the agent) moves the model on.
    await api.writeSourceFile('name-keychain', 'helper.scad', '// theirs\n')

    await retype(user, '// mine\n')
    await saveFile(user)
    const banner = await screen.findByTestId('file-changed-elsewhere')
    expect(banner).toHaveTextContent('changed elsewhere since you opened helper.scad')
    expect(await api.getDefinitionFile('name-keychain', { path: 'helper.scad' })).toBe('// theirs\n')

    // Keep editing: the next save goes over theirs.
    await user.click(within(banner).getByRole('button', { name: 'Keep editing' }))
    await saveFile(user)
    await waitFor(async () =>
      expect(await api.getDefinitionFile('name-keychain', { path: 'helper.scad' })).toBe('// mine\n'),
    )
  })

  it('reloads the file when asked, dropping the edit', async () => {
    // A model with a revision, so the save carries a base.
    await api.replaceSource('name-keychain', keychainSource)
    const { user } = renderAt('/m/name-keychain/source/helper.scad')
    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue(HELPER))
    await api.writeSourceFile('name-keychain', 'helper.scad', '// theirs\n')

    await retype(user, '// mine\n')
    await saveFile(user)
    await user.click(within(await screen.findByTestId('file-changed-elsewhere')).getByRole('button', { name: 'Reload helper.scad' }))
    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue('// theirs\n'))
    expect(screen.queryByTestId('file-changed-elsewhere')).not.toBeInTheDocument()
  })

  it('creates a new file, adding .scad, and opens it', async () => {
    const { user } = renderAt('/m/name-keychain/source')
    await files()
    await user.click(screen.getByRole('button', { name: 'New file' }))
    const dialog = await screen.findByRole('dialog', { name: 'New source file' })
    const name = within(dialog).getByLabelText('File name')

    await user.type(name, 'helper')
    expect(within(dialog).getByText('This model already has helper.scad.')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Create' })).toBeDisabled()

    await user.clear(name)
    await user.type(name, 'has space')
    expect(within(dialog).getByText(/letters, digits/)).toBeInTheDocument()

    await user.clear(name)
    await user.type(name, 'parts')
    expect(within(dialog).getByText('Saved as parts.scad.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))

    expect(await screen.findByRole('heading', { name: 'parts.scad' })).toBeInTheDocument()
    await waitFor(async () =>
      expect((await files()).getAllByRole('link').map((link) => link.textContent)).toEqual([
        'model.scad',
        'helper.scad',
        'parts.scad',
      ]),
    )
    expect(await api.getDefinitionFile('name-keychain', { path: 'parts.scad' })).toMatch(/include <parts.scad>/)
  })

  it('deletes the file after asking, and goes back to model.scad', async () => {
    const { user } = renderAt('/m/name-keychain/source/helper.scad')
    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue(HELPER))

    await user.click(await screen.findByRole('button', { name: 'Delete file' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete helper.scad?' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue(keychainSource))
    expect((await api.listSourceFiles('name-keychain')).map((file) => file.name)).toEqual(['model.scad'])
  })

  it('does not delete a file with unsaved edits', async () => {
    const { user } = renderAt('/m/name-keychain/source/helper.scad')
    await waitFor(() => expect(screen.getByLabelText('OpenSCAD source')).toHaveValue(HELPER))
    await retype(user, '// unsaved\n')
    expect(await screen.findByRole('button', { name: 'Delete file' })).toBeDisabled()
  })

  it('says so for a file that is not there', async () => {
    renderAt('/m/name-keychain/source/missing.scad')
    expect(await screen.findByRole('heading', { name: 'That file is not here' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open model.scad' })).toHaveAttribute('href', '/m/name-keychain/source')
  })

  it('offers neither New file nor Delete on a built-in', async () => {
    renderAt(`/m/${encodeURIComponent(BUILTIN_SLUG)}/source`)
    await screen.findByLabelText('OpenSCAD source')
    // A built-in with only model.scad has nothing to list or add.
    expect(screen.queryByRole('button', { name: 'New file' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete file' })).not.toBeInTheDocument()
  })
})
