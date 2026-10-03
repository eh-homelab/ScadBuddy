import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { MemoryRouter } from 'react-router'
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
    renderPage(<SessionTouched sessionId="sess-empty" />)
    expect(await screen.findByText('Nothing changed by this session yet.')).toBeInTheDocument()
  })

  it('says why when the agent cannot answer', async () => {
    server.use(
      http.get('/api/v1/ai/sessions/:id/resources', () =>
        HttpResponse.json({ detail: 'session not found' }, { status: 404 }),
      ),
    )
    renderPage(<SessionTouched sessionId="sess-gone" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('session not found')
  })

  it('reads again when the session moves on', async () => {
    setSessionResources('sess-1', [])
    const view = renderPage(<SessionTouched sessionId="sess-1" refreshKey={0} />)
    await screen.findByText('Nothing changed by this session yet.')

    setSessionResources('sess-1', [row({ type: 'model', id: 'later', model: 'later' })])
    view.rerender(
      <MemoryRouter>
        <SessionTouched sessionId="sess-1" refreshKey={1} />
      </MemoryRouter>,
    )
    expect(await screen.findByRole('link', { name: /later/ })).toBeInTheDocument()
  })
})
