import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { BROKEN_SOURCE, BUILTIN_SLUG, keychainSource } from '../mocks/fixtures'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { COPY, UPSTREAM, duplicateWithUpdate } from '../test/upstream'
import { EditSourcePage } from './EditSourcePage'

vi.mock('../components/SourceEditor', () => ({
  SourceEditor: ({
    value,
    onChange,
    label,
    readOnly,
    onSave,
  }: {
    value: string
    onChange: (next: string) => void
    label: string
    readOnly?: boolean
    onSave?: () => void
  }) => (
    <textarea
      aria-label={label}
      readOnly={readOnly}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      // The real editor binds Ctrl/Cmd+S to `onSave` (SourceEditor.test.tsx).
      onKeyDown={(event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === 's') {
          event.preventDefault()
          onSave?.()
        }
      }}
    />
  ),
}))

function renderEdit(slug = 'name-keychain', query = '') {
  const user = userEvent.setup()
  const view = render(
    <MemoryRouter initialEntries={[`/m/${slug}/source${query}`]}>
      <Routes>
        <Route path="/m/:slug/source" element={<EditSourcePage />} />
        <Route path="/m/:slug" element={<h1>Customizer</h1>} />
      </Routes>
    </MemoryRouter>,
  )
  return { user, ...view }
}

describe('EditSourcePage', () => {
  it('names the model in the breadcrumb, not its slug (#939)', async () => {
    renderEdit()
    expect(await screen.findByRole('link', { name: 'Name Keychain' })).toHaveAttribute('href', '/m/name-keychain')
  })

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
    // Save is disabled while the opening source's parse check is in flight.
    const save = screen.getByRole('button', { name: 'Save source' })
    await waitFor(() => expect(save).toBeEnabled())
    await user.click(save)

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
    const save = screen.getByRole('button', { name: 'Save source' })
    await waitFor(() => expect(save).toBeEnabled())
    await user.click(save)

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

  it('offers to save the source of a model of the user\'s own', async () => {
    renderEdit()
    expect(await screen.findByLabelText('OpenSCAD source')).not.toHaveAttribute('readonly')
    expect(screen.getByRole('button', { name: 'Save source' })).toBeInTheDocument()
    expect(screen.queryByTestId('builtin-badge')).not.toBeInTheDocument()
  })

  it('says so when the model record fails to load, and a retry brings Save back', async () => {
    let fail = true
    server.use(
      http.get('/api/v1/models/:slug', () =>
        fail
          ? HttpResponse.json({ title: 'Data directory is unreadable', status: 500 }, { status: 500 })
          : undefined,
      ),
    )
    const { user } = renderEdit()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not load this model')
    expect(screen.queryByRole('button', { name: 'Save source' })).not.toBeInTheDocument()

    fail = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByLabelText('OpenSCAD source')).toHaveValue(keychainSource)
    // Enabled once the parse check on open has settled.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save source' })).toBeEnabled())
    expect(screen.queryByText('Could not load this model')).not.toBeInTheDocument()
  })

  it('shows a built-in template read-only, with nothing to save (#184)', async () => {
    renderEdit(encodeURIComponent(BUILTIN_SLUG))
    const editor = await screen.findByLabelText('OpenSCAD source')

    expect(editor).toHaveValue(keychainSource)
    expect(editor).toHaveAttribute('readonly')
    expect(screen.getByRole('heading', { name: 'View source' })).toBeInTheDocument()
    expect(screen.getByTestId('builtin-badge')).toHaveTextContent('Built-in template — read-only')
    expect(screen.queryByRole('button', { name: 'Save source' })).not.toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Keychain Template' })).toHaveAttribute(
      'href',
      '/m/builtin%3Akeychain-template',
    )
  })

  it('duplicates a built-in to edit, landing on the copy\'s editable source (#159)', async () => {
    const { user } = renderEdit(encodeURIComponent(BUILTIN_SLUG))
    await screen.findByLabelText('OpenSCAD source')

    await user.click(screen.getByRole('button', { name: 'Duplicate to edit' }))
    const dialog = screen.getByRole('dialog', { name: 'Duplicate Keychain Template' })
    expect(within(dialog).getByRole('textbox', { name: 'Name' })).toHaveValue(
      'Keychain Template copy',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))

    expect(await screen.findByRole('heading', { name: 'Edit source' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Keychain Template copy' })).toHaveAttribute(
      'href',
      '/m/keychain-template-copy',
    )
    const editor = await screen.findByLabelText('OpenSCAD source')
    expect(editor).toHaveValue(keychainSource)
    expect(editor).not.toHaveAttribute('readonly')
    expect(screen.getByRole('button', { name: 'Save source' })).toBeInTheDocument()
    expect(screen.queryByTestId('builtin-badge')).not.toBeInTheDocument()
    // The built-in is left as it was.
    expect((await api.getModel(BUILTIN_SLUG)).origin).toBe('builtin')
  })

  it('offers no Duplicate to edit on a model of the user\'s own', async () => {
    renderEdit()
    await screen.findByRole('button', { name: 'Save source' })
    expect(screen.queryByRole('button', { name: 'Duplicate to edit' })).not.toBeInTheDocument()
  })

  it('opens a conflicted upstream merge marked up, and saves its resolution (#160)', async () => {
    await duplicateWithUpdate({ conflict: true })
    const resolve = vi.spyOn(api, 'resolveUpstreamMerge')
    const { user } = renderEdit(COPY, '?merge')

    const editor = await screen.findByLabelText('OpenSCAD source')
    expect((editor as HTMLTextAreaElement).value).toContain('<<<<<<< ')
    expect(screen.getByRole('heading', { name: 'Resolve update' })).toBeInTheDocument()
    const banner = screen.getByTestId('merge-banner')
    expect(banner).toHaveTextContent(`Resolving the update from ${UPSTREAM}`)
    expect(banner).toHaveTextContent('1 conflict left.')

    // The markers still in it are refused, whatever the parse check says.
    // Once the parse check on open has settled, so the click is not lost to it.
    await screen.findByText(/Parses cleanly/)
    const save = screen.getByRole('button', { name: 'Save resolution' })
    await user.click(save)
    expect(await screen.findByText(/still has conflict markers/)).toBeInTheDocument()

    await user.clear(editor)
    await user.click(editor)
    await user.paste('text_size = 15;\ncube(text_size);\n')
    expect(banner).toHaveTextContent('No conflicts left.')
    await screen.findByText(/Parses cleanly/)
    await user.click(save)

    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
    const revision = (await api.listVersions(UPSTREAM))[0]?.commit
    expect(resolve).toHaveBeenLastCalledWith(
      COPY,
      'text_size = 15;\ncube(text_size);\n',
      revision,
      false,
    )
    const resolved = await api.getModel(COPY)
    expect(resolved.upstream_state).toBe('current')
    expect(resolved.upstream?.base).toBe(revision)
    expect((await api.listVersions(COPY))[0]?.message).toBe(`Merge ${UPSTREAM} into ${COPY}`)
    resolve.mockRestore()
  })

  it('opens a dismissed update\'s conflicted merge marked up too (#235)', async () => {
    await duplicateWithUpdate({ conflict: true })
    await api.dismissUpstream(COPY)
    renderEdit(COPY, '?merge')

    const editor = await screen.findByLabelText('OpenSCAD source')
    expect((editor as HTMLTextAreaElement).value).toContain('<<<<<<< ')
    expect(screen.getByTestId('merge-banner')).toHaveTextContent(
      `Resolving the update from ${UPSTREAM}`,
    )
  })

  it('opens the source as it is when there is no update left to resolve (#160)', async () => {
    const replace = vi.spyOn(api, 'replaceSource')
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    const { user } = renderEdit(COPY, '?merge')

    expect(await screen.findByLabelText('OpenSCAD source')).toHaveValue(keychainSource)
    expect(screen.getByTestId('merge-banner')).toHaveTextContent('no update to resolve')
    expect(screen.getByRole('heading', { name: 'Edit source' })).toBeInTheDocument()
    await screen.findByText(/Parses cleanly/)
    await user.click(screen.getByRole('button', { name: 'Save source' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
    expect(replace).toHaveBeenCalledWith(COPY, keychainSource, false)
    replace.mockRestore()
  })
})

describe('EditSourcePage, unsaved edits (#997)', () => {
  async function edit(user: ReturnType<typeof userEvent.setup>) {
    const editor = await screen.findByLabelText('OpenSCAD source')
    await user.clear(editor)
    await user.click(editor)
    await user.paste('sphere(2);\n')
    return editor
  }

  it('asks before leaving with unsaved edits, and Stay keeps them', async () => {
    const { user } = renderEdit()
    const editor = await edit(user)

    await user.click(screen.getByRole('link', { name: 'name-keychain' }))
    const dialog = await screen.findByRole('dialog', { name: 'Leave without saving?' })
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(screen.queryByRole('heading', { name: 'Customizer' })).toBeNull()
    expect(editor).toHaveValue('sphere(2);\n')

    await user.click(screen.getByRole('link', { name: 'name-keychain' }))
    await user.click(await screen.findByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
  })

  it('leaves without asking when nothing was edited', async () => {
    const { user } = renderEdit()
    await screen.findByLabelText('OpenSCAD source')

    await user.click(screen.getByRole('link', { name: 'name-keychain' }))
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does not ask once the edits are saved', async () => {
    const { user } = renderEdit()
    await edit(user)
    const save = screen.getByRole('button', { name: 'Save source' })
    await waitFor(() => expect(save).toBeEnabled())
    await user.click(save)

    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('saves on Ctrl+S from the editor', async () => {
    const replace = vi.spyOn(api, 'replaceSource')
    const { user } = renderEdit()
    const editor = await edit(user)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save source' })).toBeEnabled())

    editor.focus()
    await user.keyboard('{Control>}s{/Control}')
    expect(await screen.findByRole('heading', { name: 'Customizer' })).toBeInTheDocument()
    expect(replace).toHaveBeenCalledWith('name-keychain', 'sphere(2);\n', false)
    replace.mockRestore()
  })

  it('does not save on Ctrl+S while Save is unavailable', async () => {
    const replace = vi.spyOn(api, 'replaceSource')
    const { user } = renderEdit(encodeURIComponent(BUILTIN_SLUG))
    const editor = await screen.findByLabelText('OpenSCAD source')

    editor.focus()
    await user.keyboard('{Control>}s{/Control}')
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(replace).not.toHaveBeenCalled()
    replace.mockRestore()
  })
})

describe('EditSourcePage, live (#269)', () => {
  const THEIRS = 'cube([3, 3, 3]);\n'

  it('follows a change made elsewhere while the buffer is untouched', async () => {
    renderEdit()
    const editor = await screen.findByLabelText('OpenSCAD source')
    await api.replaceSource('name-keychain', THEIRS)
    emitRealtime('source.changed', ['model:name-keychain'], { slug: 'name-keychain' })
    await waitFor(() => expect(editor).toHaveValue(THEIRS))
    expect(screen.queryByTestId('changed-elsewhere')).not.toBeInTheDocument()
  })

  it('never overwrites an edited buffer, and offers theirs instead', async () => {
    const { user } = renderEdit()
    const editor = await screen.findByLabelText('OpenSCAD source')
    await user.clear(editor)
    await user.click(editor)
    await user.paste('sphere(2);\n')

    await api.replaceSource('name-keychain', THEIRS)
    emitRealtime('source.changed', ['model:name-keychain'], { slug: 'name-keychain' })
    const banner = await screen.findByTestId('changed-elsewhere')
    expect(editor).toHaveValue('sphere(2);\n')

    await user.click(within(banner).getByRole('button', { name: 'Load their version' }))
    expect(editor).toHaveValue(THEIRS)
    expect(screen.queryByTestId('changed-elsewhere')).not.toBeInTheDocument()
  })

  it('takes a change that lands before the first read answers, with no banner', async () => {
    let answerFirst: (() => void) | undefined
    vi.spyOn(api, 'getSource').mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answerFirst = () => resolve(keychainSource)
        }),
    )
    renderEdit()
    await waitFor(() => expect(answerFirst).toBeDefined())
    await api.replaceSource('name-keychain', THEIRS)
    emitRealtime('source.changed', ['model:name-keychain'], { slug: 'name-keychain' })
    const editor = await screen.findByLabelText('OpenSCAD source')
    await waitFor(() => expect(editor).toHaveValue(THEIRS))
    answerFirst?.()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(editor).toHaveValue(THEIRS)
    expect(screen.queryByTestId('changed-elsewhere')).not.toBeInTheDocument()
  })
})
