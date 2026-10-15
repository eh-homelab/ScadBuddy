import { act, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { api } from '../../api/client'
import type { SessionResource } from '../../api/types'
import { setSessionResources } from '../../mocks/features/assistantSessions'
import { versionIds } from '../../mocks/fixtures'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SessionTouched } from './SessionTouched'

// #931: what a session's tool calls touched, from GET /api/v1/ai/sessions/:id/resources
// (agent `sessions/touched.ts` TouchedRecord), grouped by kind, each linking to its page.

const at = '2026-10-03T12:00:00.000Z'
const row = (fields: Partial<SessionResource> & Pick<SessionResource, 'type' | 'id'>): SessionResource => ({
  action: 'created',
  model: null,
  before: null,
  after: null,
  tool: 'some_tool',
  at,
  ...fields,
})

const group = (name: string) => screen.getByRole('group', { name })

describe('SessionTouched', () => {
  it('lists libraries, fonts, settings and Bambuddy items, linking those ScadBuddy has a page for', async () => {
    setSessionResources('sess-k', [
      row({ type: 'library', id: 'BOSL2', model: 'my-bin', tool: 'pin_library' }),
      row({ type: 'library', id: 'old-lib', action: 'deleted', tool: 'remove_library_checkout' }),
      row({ type: 'font', id: 'Lobster Two', tool: 'install_font' }),
      row({ type: 'setting', id: 'print_options:global', action: 'modified', tool: 'set_print_options' }),
      row({ type: 'project', id: '9', tool: 'create_print_project' }),
      row({ type: 'bambuddy_file', id: '31', before: 'out-7', tool: 'send_to_bambuddy' }),
      row({ type: 'print_archive', id: '7', action: 'modified', tool: 'pull_print_timelapse' }),
    ])
    renderPage(<SessionTouched sessionId="sess-k" />)

    const libraries = await screen.findByRole('group', { name: 'Libraries' })
    expect(within(libraries).getByRole('link', { name: /BOSL2/ })).toHaveAttribute('href', '/m/my-bin')
    // A removed checkout: gone, so no link.
    expect(within(libraries).queryByRole('link', { name: /old-lib/ })).not.toBeInTheDocument()
    expect(within(libraries).getByText('old-lib')).toBeInTheDocument()
    expect(within(group('Fonts')).getByText('Lobster Two')).toBeInTheDocument()
    expect(within(group('Settings')).getByRole('link', { name: 'print_options:global' })).toHaveAttribute('href', '/settings')
    expect(within(group('Bambuddy projects')).getByText('project 9')).toBeInTheDocument()
    expect(within(group('Bambuddy files')).getByText('library file 31')).toBeInTheDocument()
    expect(within(group('Print archives')).getByRole('link', { name: 'print 7' })).toHaveAttribute('href', '/prints/7')
  })

  it("keeps one library's pins on different models, and its checkout's removal, apart", async () => {
    setSessionResources('sess-lib', [
      row({ type: 'library', id: 'BOSL2', model: 'bin-a', tool: 'pin_library' }),
      row({ type: 'library', id: 'BOSL2', model: 'bin-b', tool: 'pin_library' }),
      row({ type: 'library', id: 'BOSL2', model: 'bin-a', action: 'deleted', tool: 'unpin_library' }),
      row({ type: 'library', id: 'BOSL2', model: null, before: 'c'.repeat(40), action: 'deleted', tool: 'remove_library_checkout' }),
    ])
    renderPage(<SessionTouched sessionId="sess-lib" />)
    const libraries = await screen.findByRole('group', { name: 'Libraries' })
    const items = within(libraries).getAllByRole('listitem')
    expect(items).toHaveLength(3)
    // Pinned on bin-b and still pinned: linked to that model.
    expect(within(libraries).getByRole('link', { name: 'BOSL2 · bin-b' })).toHaveAttribute('href', '/m/bin-b')
    // Pinned then unpinned on bin-a; the shared checkout removed: neither is linked.
    expect(within(libraries).getByText('BOSL2 · bin-a')).toBeInTheDocument()
    expect(within(libraries).queryByRole('link', { name: 'BOSL2 · bin-a' })).not.toBeInTheDocument()
    expect(items[2]).toHaveTextContent(/^BOSL2deleted$/)
  })

  it("links a library's shared checkout, while it is installed, to Settings' Libraries", async () => {
    setSessionResources('sess-lib2', [row({ type: 'library', id: 'MCAD', model: null, action: 'modified', tool: 'x' })])
    renderPage(<SessionTouched sessionId="sess-lib2" />)
    const libraries = await screen.findByRole('group', { name: 'Libraries' })
    expect(within(libraries).getByRole('link', { name: 'MCAD' })).toHaveAttribute('href', '/settings#libraries')
  })

  it('groups what the session touched by kind, each linking to its page', async () => {
    setSessionResources('sess-1', [
      row({ type: 'model', id: 'my-bin', model: 'my-bin', tool: 'create_model', after: 'aaaaaaa1' }),
      row({ type: 'revision', id: '0123456789abcdef', model: 'my-bin', before: 'aaaaaaa1', after: '0123456789abcdef', tool: 'apply_patch' }),
      row({ type: 'preset', id: 'preset-42', model: 'my-bin', tool: 'save_preset' }),
      row({ type: 'output', id: 'out-7', model: 'my-bin', tool: 'save_output' }),
      row({ type: 'print_run', id: 'run-3', before: 'out-7', tool: 'print_output' }),
      row({ type: 'print', id: '991', before: 'out-7', tool: 'print_output' }),
    ])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const models = await screen.findByRole('group', { name: 'Models' })
    expect(within(models).getByRole('link', { name: /my-bin/ })).toHaveAttribute('href', '/m/my-bin')
    expect(within(group('Revisions')).getByRole('link', { name: /0123456/ })).toHaveAttribute(
      'href',
      '/m/my-bin?version=0123456789abcdef',
    )
    expect(within(group('Presets')).getByRole('link', { name: /preset-42/ })).toHaveAttribute('href', '/m/my-bin')
    expect(within(group('Outputs')).getByRole('link', { name: /out-7/ })).toHaveAttribute('href', '/edit/out-7')
    expect(within(group('Print runs')).getByRole('link', { name: /run-3/ })).toHaveAttribute('href', '/prints')
    expect(within(group('Prints')).getByRole('link', { name: /991/ })).toHaveAttribute('href', '/prints')
    expect(screen.queryByRole('group', { name: 'Assets' })).toBeNull()
  })

  it('says how each was touched, once per resource, and links nothing that is gone', async () => {
    setSessionResources('sess-1', [
      row({ type: 'preset', id: 'p-1', model: 'bin', tool: 'save_preset' }),
      row({ type: 'preset', id: 'p-1', model: 'bin', action: 'modified', tool: 'update_preset' }),
      row({ type: 'output', id: 'out-1', model: 'bin', action: 'deleted', tool: 'delete_output' }),
    ])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const presets = await screen.findByRole('group', { name: 'Presets' })
    expect(within(presets).getAllByRole('listitem')).toHaveLength(1)
    expect(within(presets).getByRole('listitem')).toHaveTextContent(/created, changed/)
    const outputs = group('Outputs')
    expect(within(outputs).getByRole('listitem')).toHaveTextContent(/out-1.*deleted/)
    expect(within(outputs).queryByRole('link')).toBeNull()
  })

  it('lists a write it cannot classify yet by its tool', async () => {
    setSessionResources('sess-1', [row({ type: 'unclassified', id: null, tool: 'set_choice' })])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const other = await screen.findByRole('group', { name: 'Other changes' })
    expect(other).toHaveTextContent('set_choice')
    expect(within(other).queryByRole('link')).toBeNull()
  })

  it('says when the session has touched nothing', async () => {
    setSessionResources('sess-empty', [])
    renderPage(<SessionTouched sessionId="sess-empty" />)
    expect(await screen.findByText('Nothing changed by this session yet.')).toBeInTheDocument()
  })

  it('says why when the agent cannot answer', async () => {
    // The mock refuses a session it does not know, as the real route does.
    renderPage(<SessionTouched sessionId="sess-gone" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('session not found')
  })

  it('reads again when the session moves on, keeping the list on screen meanwhile', async () => {
    setSessionResources('sess-1', [row({ type: 'model', id: 'first', model: 'first' })])
    const view = renderPage(<SessionTouched sessionId="sess-1" refreshKey="running" />)
    await screen.findByRole('link', { name: /first/ })

    let answer: () => void = () => {}
    const answered = new Promise<void>((resolve) => (answer = resolve))
    let asked: () => void = () => {}
    const reread = new Promise<void>((resolve) => (asked = resolve))
    server.use(
      http.get('/api/v1/ai/sessions/:id/resources', async () => {
        asked()
        await answered
        return HttpResponse.json({ resources: [row({ type: 'model', id: 'later', model: 'later' })] })
      }),
    )
    view.rerender(<SessionTouched sessionId="sess-1" refreshKey="idle" />)
    await reread
    expect(screen.getByRole('link', { name: /first/ })).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).toBeNull()

    answer()
    expect(await screen.findByRole('link', { name: /later/ })).toBeInTheDocument()
  })

  it('keeps the list when a re-read fails', async () => {
    setSessionResources('sess-1', [row({ type: 'model', id: 'first', model: 'first' })])
    const view = renderPage(<SessionTouched sessionId="sess-1" refreshKey="running" />)
    await screen.findByRole('link', { name: /first/ })

    server.use(
      http.get('/api/v1/ai/sessions/:id/resources', () => HttpResponse.json({ detail: 'down' }, { status: 503 })),
    )
    const reads = vi.spyOn(api, 'listAiSessionResources')
    view.rerender(<SessionTouched sessionId="sess-1" refreshKey="idle" />)
    await vi.waitFor(() => expect(reads).toHaveBeenCalledTimes(1))
    // useAsync's handlers were attached when it called; once this settles, theirs has run.
    await act(async () => {
      await reads.mock.results[0]!.value.catch(() => {})
    })
    expect(screen.getByRole('link', { name: /first/ })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    reads.mockRestore()
  })

  it('shows every action in the order it happened, and links a resource made again after a delete', async () => {
    setSessionResources('sess-1', [
      row({ type: 'model', id: 'bin', model: 'bin', tool: 'create_model' }),
      row({ type: 'model', id: 'bin', model: 'bin', action: 'deleted', tool: 'delete_model' }),
      row({ type: 'model', id: 'bin', model: 'bin', tool: 'create_from_template' }),
    ])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const item = within(await screen.findByRole('group', { name: 'Models' })).getByRole('listitem')
    expect(item).toHaveTextContent('created, deleted, created')
    expect(within(item).getByRole('link', { name: /bin/ })).toHaveAttribute('href', '/m/bin')
  })

  it('links nothing on a model the session deleted', async () => {
    setSessionResources('sess-1', [
      row({ type: 'revision', id: 'abcdef0123456', model: 'bin', tool: 'update_source' }),
      row({ type: 'preset', id: 'p-1', model: 'bin', tool: 'save_preset' }),
      row({ type: 'model', id: 'bin', model: 'bin', action: 'deleted', tool: 'delete_model' }),
    ])
    renderPage(<SessionTouched sessionId="sess-1" />)

    await screen.findByRole('group', { name: 'Revisions' })
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('does not link what belonged to a model the session deleted and made again', async () => {
    setSessionResources('sess-1', [
      row({ type: 'revision', id: 'abcdef0123456', model: 'bin', tool: 'update_source' }),
      row({ type: 'model', id: 'bin', model: 'bin', action: 'deleted', tool: 'delete_model' }),
      row({ type: 'model', id: 'bin', model: 'bin', tool: 'create_model' }),
      row({ type: 'preset', id: 'p-new', model: 'bin', tool: 'save_preset' }),
    ])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const revisions = await screen.findByRole('group', { name: 'Revisions' })
    expect(within(revisions).queryByRole('link')).toBeNull()
    expect(within(group('Models')).getByRole('link', { name: /bin/ })).toHaveAttribute('href', '/m/bin')
    expect(within(group('Presets')).getByRole('link', { name: /p-new/ })).toHaveAttribute('href', '/m/bin')
  })

  it('shortens only a revision id; an output keeps its whole id', async () => {
    const outputId = '0f1e2d3c4b5a69788796a5b4c3d2e1f0'
    setSessionResources('sess-1', [
      row({ type: 'revision', id: '0123456789abcdef', model: 'bin' }),
      row({ type: 'output', id: outputId, model: 'bin', tool: 'save_output' }),
    ])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const outputs = await screen.findByRole('group', { name: 'Outputs' })
    expect(within(outputs).getByRole('link', { name: new RegExp(outputId) })).toHaveAttribute('href', `/edit/${outputId}`)
    expect(within(group('Revisions')).getByRole('link')).toHaveTextContent(/^0123456 · bin$/)
  })

  it('sums up the session in one plain line', async () => {
    setSessionResources('sess-sum', [
      row({ type: 'revision', id: 'a'.repeat(40), model: 'keychain', tool: 'update_source' }),
      row({ type: 'revision', id: 'b'.repeat(40), model: 'keychain', tool: 'apply_patch' }),
      row({ type: 'revision', id: 'c'.repeat(40), model: 'bin', tool: 'apply_patch' }),
      row({ type: 'render_job', id: 'job-1', model: 'keychain', tool: 'render_model' }),
      row({ type: 'output', id: 'out-1', model: 'keychain', tool: 'save_output' }),
      // One print: its run and its queue item.
      row({ type: 'print_run', id: 'run-1', before: 'out-1', tool: 'print_output' }),
      row({ type: 'print', id: '77', before: 'out-1', tool: 'print_output' }),
      row({ type: 'setting', id: 'last_project', action: 'modified', tool: 'set_last_project' }),
      row({ type: 'unclassified', id: null, tool: 'browser_click' }),
    ])
    renderPage(<SessionTouched sessionId="sess-sum" />)
    expect(
      await screen.findByText(
        '2 new versions of keychain, 1 new version of bin, 1 render, 1 output saved, 1 print queued, 2 other changes',
      ),
    ).toBeInTheDocument()
  })

  it('counts a print run as the print when no queue item was recorded', async () => {
    setSessionResources('sess-run', [
      row({ type: 'model', id: 'bin', model: 'bin', tool: 'create_model' }),
      row({ type: 'print_run', id: 'run-1', before: 'out-1', tool: 'print_output' }),
    ])
    renderPage(<SessionTouched sessionId="sess-run" />)
    expect(await screen.findByText('1 new model, 1 print queued')).toBeInTheDocument()
  })

  it('puts a model back to how it was before the session, as a new version, after a confirm', async () => {
    const user = userEvent.setup()
    setSessionResources('sess-undo', [
      row({ type: 'revision', id: versionIds.raised, model: 'name-keychain', before: versionIds.edited, tool: 'apply_patch' }),
      row({ type: 'revision', id: 'f'.repeat(40), model: 'name-keychain', before: versionIds.raised, tool: 'apply_patch' }),
    ])
    const restore = vi.spyOn(api, 'restoreVersion')
    renderPage(<SessionTouched sessionId="sess-undo" />)

    await user.click(await screen.findByRole('button', { name: 'Undo changes to name-keychain' }))
    const dialog = await screen.findByRole('dialog', { name: "Undo this chat's changes to name-keychain?" })
    expect(dialog).toHaveTextContent('saved as a new version')
    await user.click(within(dialog).getByRole('button', { name: 'Undo changes' }))

    expect(await screen.findByRole('status')).toHaveTextContent('name-keychain is back to how it was before this chat.')
    // The version before the session's first change, not the one between its changes.
    expect(restore).toHaveBeenCalledWith('name-keychain', versionIds.edited)
    restore.mockRestore()
  })

  it('says why an undo failed, and keeps the dialog open', async () => {
    const user = userEvent.setup()
    setSessionResources('sess-undo', [
      row({ type: 'revision', id: 'f'.repeat(40), model: 'name-keychain', before: 'e'.repeat(40), tool: 'apply_patch' }),
    ])
    renderPage(<SessionTouched sessionId="sess-undo" />)

    await user.click(await screen.findByRole('button', { name: 'Undo changes to name-keychain' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Undo changes' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Revision not found')
  })

  it('offers no undo for a model the session made or deleted, or a change with no earlier version', async () => {
    setSessionResources('sess-none', [
      row({ type: 'model', id: 'new-bin', model: 'new-bin', tool: 'create_model' }),
      row({ type: 'revision', id: 'a'.repeat(40), model: 'new-bin', before: 'b'.repeat(40), tool: 'apply_patch' }),
      row({ type: 'revision', id: 'c'.repeat(40), model: 'gone', before: 'd'.repeat(40), tool: 'apply_patch' }),
      row({ type: 'model', id: 'gone', model: 'gone', action: 'deleted', tool: 'delete_model' }),
      row({ type: 'revision', id: 'e'.repeat(40), model: 'old', before: null, tool: 'update_source' }),
    ])
    renderPage(<SessionTouched sessionId="sess-none" />)
    await screen.findByRole('group', { name: 'Revisions' })
    expect(screen.queryByRole('button', { name: /Undo changes/ })).toBeNull()
  })

  it('offers no undo when the session changed the model before its first new version', async () => {
    setSessionResources('sess-details', [
      row({ type: 'model', id: 'name-keychain', model: 'name-keychain', action: 'modified', tool: 'update_model_details' }),
      row({ type: 'revision', id: 'f'.repeat(40), model: 'name-keychain', before: versionIds.raised, tool: 'apply_patch' }),
      // A details change after the first new version is undone with it.
      row({ type: 'revision', id: 'a'.repeat(40), model: 'bin', before: 'b'.repeat(40), tool: 'apply_patch' }),
      row({ type: 'model', id: 'bin', model: 'bin', action: 'modified', tool: 'update_model_details' }),
    ])
    server.use(http.get('/api/v1/models/:slug', () => HttpResponse.json({ slug: 'bin', version: null })))
    renderPage(<SessionTouched sessionId="sess-details" />)
    expect(await screen.findByRole('button', { name: 'Undo changes to bin' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Undo changes to name-keychain' })).toBeNull()
  })

  it('offers no undo once the model is already back to how it was', async () => {
    const before = 'b'.repeat(40)
    const restored = 'c'.repeat(40)
    setSessionResources('sess-back', [
      row({ type: 'revision', id: 'a'.repeat(40), after: 'a'.repeat(40), model: 'bin', before, tool: 'apply_patch' }),
      row({ type: 'revision', id: 'd'.repeat(40), after: 'd'.repeat(40), model: 'gone-back', before, tool: 'apply_patch' }),
    ])
    server.use(
      http.get('/api/v1/models/:slug', ({ params }) =>
        HttpResponse.json({ slug: params.slug, version: params.slug === 'bin' ? before : restored }),
      ),
      // gone-back's newest version is a restore of `before`: nothing between them.
      http.get('/api/v1/models/:slug/versions/:commit/diff', () => HttpResponse.json({ patch: '', files: [] })),
    )
    const reads = vi.spyOn(api, 'getVersionDiff')
    renderPage(<SessionTouched sessionId="sess-back" />)
    await screen.findByRole('group', { name: 'Revisions' })
    await vi.waitFor(() => expect(reads).toHaveBeenCalledWith('gone-back', restored, before))
    const call = reads.mock.calls.findIndex(([slug]) => slug === 'gone-back')
    await act(async () => {
      await reads.mock.results[call]!.value
    })
    expect(screen.queryByRole('button', { name: /Undo changes/ })).toBeNull()
    reads.mockRestore()
  })

  it('warns that an undo also takes back changes made after the session', async () => {
    const user = userEvent.setup()
    setSessionResources('sess-moved', [
      row({ type: 'revision', id: 'a'.repeat(40), after: 'a'.repeat(40), model: 'bin', before: 'b'.repeat(40), tool: 'apply_patch' }),
    ])
    server.use(
      http.get('/api/v1/models/:slug', () => HttpResponse.json({ slug: 'bin', version: 'e'.repeat(40) })),
      http.get('/api/v1/models/:slug/versions/:commit/diff', () => HttpResponse.json({ patch: '-a\n+b\n', files: [{ status: 'M', path: 'model.scad' }] })),
    )
    renderPage(<SessionTouched sessionId="sess-moved" />)
    await user.click(await screen.findByRole('button', { name: 'Undo changes to bin' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('It was also changed outside this chat')
  })

  it('warns when the model was changed between two of the session’s own versions', async () => {
    const user = userEvent.setup()
    setSessionResources('sess-between', [
      row({ type: 'revision', id: 'a'.repeat(40), after: 'a'.repeat(40), model: 'bin', before: 'b'.repeat(40), tool: 'apply_patch' }),
      // Its parent is not the session's previous version: someone else edited in between.
      row({ type: 'revision', id: 'c'.repeat(40), after: 'c'.repeat(40), model: 'bin', before: 'e'.repeat(40), tool: 'apply_patch' }),
    ])
    server.use(
      http.get('/api/v1/models/:slug', () => HttpResponse.json({ slug: 'bin', version: 'c'.repeat(40) })),
      http.get('/api/v1/models/:slug/versions/:commit/diff', () => HttpResponse.json({ patch: '-a\n+b\n', files: [{ status: 'M', path: 'model.scad' }] })),
    )
    renderPage(<SessionTouched sessionId="sess-between" />)
    await user.click(await screen.findByRole('button', { name: 'Undo changes to bin' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('It was also changed outside this chat')
  })

  it('does not warn when a later version names its parent by a short id', async () => {
    const user = userEvent.setup()
    setSessionResources('sess-short', [
      row({ type: 'revision', id: 'a'.repeat(40), after: 'a'.repeat(40), model: 'bin', before: 'b'.repeat(40), tool: 'apply_patch' }),
      // `before` is the call's `base`, which may be the short form of the previous version.
      row({ type: 'revision', id: 'c'.repeat(40), after: 'c'.repeat(40), model: 'bin', before: 'a'.repeat(7), tool: 'apply_patch' }),
    ])
    server.use(
      http.get('/api/v1/models/:slug', () => HttpResponse.json({ slug: 'bin', version: 'c'.repeat(40) })),
      http.get('/api/v1/models/:slug/versions/:commit/diff', () =>
        HttpResponse.json({ patch: '-a\n+b\n', files: [{ status: 'M', path: 'model.scad' }] }),
      ),
    )
    renderPage(<SessionTouched sessionId="sess-short" />)
    await user.click(await screen.findByRole('button', { name: 'Undo changes to bin' }))
    expect(await screen.findByRole('dialog')).not.toHaveTextContent('It was also changed outside this chat')
  })

  it('offers the undo when only a file with no text diff changed, such as a thumbnail', async () => {
    setSessionResources('sess-binary', [
      row({ type: 'revision', id: 'a'.repeat(40), after: 'a'.repeat(40), model: 'bin', before: 'b'.repeat(40), tool: 'set_thumbnail' }),
    ])
    server.use(
      http.get('/api/v1/models/:slug', () => HttpResponse.json({ slug: 'bin', version: 'a'.repeat(40) })),
      http.get('/api/v1/models/:slug/versions/:commit/diff', () =>
        HttpResponse.json({ patch: '', files: [{ status: 'M', path: 'thumbnail.png' }] }),
      ),
    )
    renderPage(<SessionTouched sessionId="sess-binary" />)
    expect(await screen.findByRole('button', { name: 'Undo changes to bin' })).toBeInTheDocument()
  })

  it('offers the undo again when the session changes the model after an undo', async () => {
    const user = userEvent.setup()
    const first = [
      row({ type: 'revision', id: versionIds.raised, after: versionIds.raised, model: 'name-keychain', before: versionIds.edited, tool: 'apply_patch' }),
    ]
    setSessionResources('sess-again', first)
    const view = renderPage(<SessionTouched sessionId="sess-again" refreshKey={1} />)
    await user.click(await screen.findByRole('button', { name: 'Undo changes to name-keychain' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Undo changes' }))
    await screen.findByRole('status')

    setSessionResources('sess-again', [
      ...first,
      row({ type: 'revision', id: 'f'.repeat(40), after: 'f'.repeat(40), model: 'name-keychain', before: 'e'.repeat(40), tool: 'apply_patch' }),
    ])
    view.rerender(<SessionTouched sessionId="sess-again" refreshKey={2} />)
    expect(await screen.findByRole('button', { name: 'Undo changes to name-keychain' })).toBeInTheDocument()
  })

  it('holds the undo back while the assistant is working', async () => {
    setSessionResources('sess-working', [
      row({ type: 'revision', id: 'a'.repeat(40), model: 'bin', before: 'b'.repeat(40), tool: 'apply_patch' }),
    ])
    const view = renderPage(<SessionTouched sessionId="sess-working" working />)
    expect(await screen.findByText(/available when the assistant has finished/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Undo changes/ })).toBeNull()
    view.rerender(<SessionTouched sessionId="sess-working" />)
    expect(await screen.findByRole('button', { name: 'Undo changes to bin' })).toBeInTheDocument()
  })

  it('counts a model made and deleted in the session as deleted', async () => {
    setSessionResources('sess-made-gone', [
      row({ type: 'model', id: 'tmp', model: 'tmp', tool: 'create_model' }),
      row({ type: 'model', id: 'tmp', model: 'tmp', action: 'deleted', tool: 'delete_model' }),
    ])
    renderPage(<SessionTouched sessionId="sess-made-gone" />)
    expect(await screen.findByText('1 model deleted')).toBeInTheDocument()
  })

  it('still offers the undo when the model cannot be read, saying it could not check', async () => {
    const user = userEvent.setup()
    setSessionResources('sess-unread', [
      row({ type: 'revision', id: 'a'.repeat(40), model: 'bin', before: 'b'.repeat(40), tool: 'apply_patch' }),
    ])
    server.use(http.get('/api/v1/models/:slug', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    renderPage(<SessionTouched sessionId="sess-unread" />)
    await user.click(await screen.findByRole('button', { name: 'Undo changes to bin' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Could not check whether it was changed outside this chat.')
    expect(dialog).not.toHaveTextContent('It was also changed outside this chat')
  })

  it('counts a new version of no known model in the summary', async () => {
    setSessionResources('sess-anon', [row({ type: 'revision', id: 'a'.repeat(40), model: null, tool: 'update_source' })])
    renderPage(<SessionTouched sessionId="sess-anon" />)
    expect(await screen.findByText('1 other new version')).toBeInTheDocument()
  })

  it('does not link a revision whose model is unknown', async () => {
    setSessionResources('sess-1', [row({ type: 'revision', id: 'abcdef0123456', model: null })])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const revisions = await screen.findByRole('group', { name: 'Revisions' })
    expect(revisions).toHaveTextContent('abcdef0')
    expect(within(revisions).queryByRole('link')).toBeNull()
  })
})
