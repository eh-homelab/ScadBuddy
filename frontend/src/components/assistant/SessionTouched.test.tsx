import { act, screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { MemoryRouter } from 'react-router'
import { api } from '../../api/client'
import type { SessionResource } from '../../api/types'
import { setSessionResources } from '../../mocks/features/assistantSessions'
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
    view.rerender(
      <MemoryRouter>
        <SessionTouched sessionId="sess-1" refreshKey="idle" />
      </MemoryRouter>,
    )
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
    view.rerender(
      <MemoryRouter>
        <SessionTouched sessionId="sess-1" refreshKey="idle" />
      </MemoryRouter>,
    )
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

  it('does not link a revision whose model is unknown', async () => {
    setSessionResources('sess-1', [row({ type: 'revision', id: 'abcdef0123456', model: null })])
    renderPage(<SessionTouched sessionId="sess-1" />)

    const revisions = await screen.findByRole('group', { name: 'Revisions' })
    expect(revisions).toHaveTextContent('abcdef0')
    expect(within(revisions).queryByRole('link')).toBeNull()
  })
})
