import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { AssistantOpenerContext } from '../../agent/chat/opener'
import type { ResourceRef, SessionResource } from '../../api/types'
import { setSessionResources } from '../../mocks/features/assistantSessions'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ResourceSessions } from './ResourceSessions'

// #931: the sessions whose tool calls touched a resource, from
// GET /api/v1/ai/resources/:type/:id/sessions, each opening that session in the panel.

const row = (fields: Partial<SessionResource> & Pick<SessionResource, 'type' | 'id'>): SessionResource => ({
  action: 'created',
  model: null,
  before: null,
  after: null,
  tool: 'some_tool',
  at: '2026-10-03T12:00:00.000Z',
  ...fields,
})

function renderWith(resource: ResourceRef, openSession = vi.fn(), label?: string) {
  const view = renderPage(
    <AssistantOpenerContext.Provider value={{ openSession }}>
      <ResourceSessions resource={resource} {...(label ? { label } : {})} />
    </AssistantOpenerContext.Provider>,
  )
  return { ...view, openSession }
}

describe('ResourceSessions', () => {
  it('lists the sessions that touched the model, newest first, and opens the one picked', async () => {
    setSessionResources('sess-old', [row({ type: 'model', id: 'my-bin', model: 'my-bin' })], {
      title: 'Make the bin',
      updated_at: '2026-10-01T10:00:00.000Z',
    })
    setSessionResources('sess-new', [row({ type: 'preset', id: 'p1', model: 'my-bin' })], {
      title: '',
      status: 'running',
      updated_at: '2026-10-03T10:00:00.000Z',
    })
    setSessionResources('sess-else', [row({ type: 'model', id: 'other', model: 'other' })], { title: 'Elsewhere' })
    const { user, openSession } = renderWith({ type: 'model', id: 'my-bin' })

    const toggle = await screen.findByRole('button', { name: 'Changed by assistant (2)' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    const menu = screen.getByRole('menu', { name: 'Assistant sessions that changed this' })
    const items = within(menu).getAllByRole('menuitem')
    expect(items.map((i) => i.textContent)).toEqual([
      expect.stringMatching(/^Untitled session.*Working/),
      expect.stringMatching(/^Make the bin.*Idle/),
    ])
    expect(within(menu).queryByText('Elsewhere')).not.toBeInTheDocument()

    await user.click(items[1]!)
    expect(openSession).toHaveBeenCalledWith('sess-old')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('looks an output up by its own id, under the label given', async () => {
    setSessionResources('sess-out', [row({ type: 'output', id: 'out/7', model: 'my-bin' })], { title: 'Saved it' })
    const requests: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.url.includes('/ai/resources/')) requests.push(new URL(request.url).pathname)
    })
    const { user } = renderWith({ type: 'output', id: 'out/7' }, vi.fn(), 'Output changed by assistant')
    await user.click(await screen.findByRole('button', { name: 'Output changed by assistant (1)' }))
    expect(screen.getByRole('menuitem', { name: /Saved it/ })).toBeInTheDocument()
    expect(requests).toEqual(['/api/v1/ai/resources/output/out%2F7/sessions'])
    server.events.removeAllListeners()
  })

  it('shows nothing when no session touched it, or the agent cannot say', async () => {
    let answered = false
    server.use(
      http.get('/api/v1/ai/resources/:type/:id/sessions', () => {
        answered = true
        return HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 })
      }),
    )
    const { container } = renderWith({ type: 'model', id: 'my-bin' })
    await waitFor(() => expect(answered).toBe(true))
    expect(container).toBeEmptyDOMElement()

    server.resetHandlers()
    const empty = renderWith({ type: 'model', id: 'never-touched' })
    await waitFor(() => expect(empty.container).toBeEmptyDOMElement())
    expect(screen.queryByRole('button', { name: /Changed by assistant/ })).not.toBeInTheDocument()
  })

  it('reads nothing and shows nothing without the assistant', async () => {
    let asked = false
    server.use(
      http.get('/api/v1/ai/resources/:type/:id/sessions', () => {
        asked = true
        return HttpResponse.json({ sessions: [] })
      }),
    )
    const { container } = renderPage(<ResourceSessions resource={{ type: 'model', id: 'gridfinity-bin' }} />)
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(container).toBeEmptyDOMElement()
    expect(asked).toBe(false)
  })
})
