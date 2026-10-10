import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'

// #1920 — typing "/" at the start of the composer lists the skills the assistant loads
// (the plugin list's enabled built-ins and packages, src/mocks/aiPlugins.ts), to insert one.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

let agent: MockAgentTransport
const factory = () => {
  agent = createMockAgentTransport({ stepMs: 0 })
  return agent
}

const sentMessages = () =>
  agent.sent.filter((m): m is Extract<ClientMessage, { type: 'user.message' }> => m.type === 'user.message')

async function openComposer() {
  const view = renderPage(
    <Routes>
      <Route element={<AppShell embedded={false} assistantTransport={factory} />}>
        <Route path="*" element={<p>page</p>} />
      </Route>
    </Routes>,
    { route: '/m/name-keychain' },
  )
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
  return { view, box }
}

const optionNames = (list: HTMLElement) => within(list).getAllByRole('option').map((o) => o.textContent)

describe('the "/" skill menu (#1920)', () => {
  it('lists the loaded skills when the draft starts with "/", wired to the composer', async () => {
    const { view, box } = await openComposer()
    expect(screen.queryByRole('listbox', { name: 'Skills' })).toBeNull()
    expect(box).toHaveAttribute('aria-autocomplete', 'list')

    await view.user.type(box, '/')
    const list = await screen.findByRole('listbox', { name: 'Skills' })
    await waitFor(() =>
      expect(optionNames(list)).toEqual(['/scadbuddy:authoring', '/scadbuddy:customize', '/scadbuddy:print']),
    )
    expect(box).toHaveAttribute('aria-controls', list.id)
    const first = within(list).getAllByRole('option')[0]!
    expect(first).toHaveAttribute('aria-selected', 'true')
    expect(box).toHaveAttribute('aria-activedescendant', first.id)
    expect(screen.getByRole('status', { name: 'Skill suggestions' })).toHaveTextContent(
      '3 skills. Up and down arrows to choose, Enter to insert, Escape to close.',
    )
  })

  it('filters as you type and closes once the draft is no longer one "/" word', async () => {
    const { view, box } = await openComposer()
    await view.user.type(box, '/pri')
    const list = await screen.findByRole('listbox', { name: 'Skills' })
    await waitFor(() => expect(optionNames(list)).toEqual(['/scadbuddy:print']))
    expect(screen.getByRole('status', { name: 'Skill suggestions' })).toHaveTextContent('1 skill.')

    await view.user.type(box, 'zz')
    await waitFor(() => expect(within(list).queryAllByRole('option')).toHaveLength(0))
    expect(within(list).getByText('No skill matches “/prizz”.')).toBeInTheDocument()

    await view.user.type(box, ' ')
    expect(screen.queryByRole('listbox', { name: 'Skills' })).toBeNull()
    await view.user.clear(box)
    await view.user.type(box, 'say /print')
    expect(screen.queryByRole('listbox', { name: 'Skills' })).toBeNull()
  })

  it('moves with the arrow keys, inserts the invocation on Enter, and sends nothing', async () => {
    const { view, box } = await openComposer()
    await view.user.type(box, '/')
    const list = await screen.findByRole('listbox', { name: 'Skills' })
    await waitFor(() => expect(within(list).getAllByRole('option')).toHaveLength(3))

    await view.user.keyboard('{ArrowDown}{ArrowDown}')
    const third = within(list).getByRole('option', { name: '/scadbuddy:print' })
    expect(third).toHaveAttribute('aria-selected', 'true')
    expect(box).toHaveAttribute('aria-activedescendant', third.id)
    expect(screen.getByRole('status', { name: 'Skill suggestions' })).toHaveTextContent('/scadbuddy:print, 3 of 3')
    await view.user.keyboard('{ArrowDown}')
    expect(within(list).getByRole('option', { name: '/scadbuddy:authoring' })).toHaveAttribute('aria-selected', 'true')
    await view.user.keyboard('{ArrowUp}')
    expect(third).toHaveAttribute('aria-selected', 'true')

    await view.user.keyboard('{Enter}')
    expect(box).toHaveValue('/scadbuddy:print ')
    expect(box).toHaveFocus()
    expect(screen.queryByRole('listbox', { name: 'Skills' })).toBeNull()
    expect(sentMessages()).toHaveLength(0)
    expect(screen.getByRole('status', { name: 'Skill suggestions' })).toHaveTextContent('Inserted /scadbuddy:print.')

    // The message goes when the user sends it, as any other.
    await view.user.type(box, 'my keychain{Enter}')
    await waitFor(() => expect(sentMessages().map((m) => m.text)).toEqual(['/scadbuddy:print my keychain']))
  })

  it('inserts a skill on click', async () => {
    const { view, box } = await openComposer()
    await view.user.type(box, '/c')
    const option = await screen.findByRole('option', { name: '/scadbuddy:customize' })
    await view.user.click(option)
    expect(box).toHaveValue('/scadbuddy:customize ')
    expect(box).toHaveFocus()
    expect(sentMessages()).toHaveLength(0)
  })

  it('closes on Escape, keeps the draft, and stays closed until the "/" is typed again', async () => {
    const { view, box } = await openComposer()
    await view.user.type(box, '/cu')
    await screen.findByRole('listbox', { name: 'Skills' })
    await view.user.keyboard('{Escape}')
    expect(screen.queryByRole('listbox', { name: 'Skills' })).toBeNull()
    expect(box).toHaveValue('/cu')
    // The panel is still open: Escape went to the menu only.
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toBe(box)
    await view.user.type(box, 's')
    expect(screen.queryByRole('listbox', { name: 'Skills' })).toBeNull()

    await view.user.clear(box)
    await view.user.type(box, '/')
    expect(await screen.findByRole('listbox', { name: 'Skills' })).toBeInTheDocument()
  })

  it('sends on Enter as before when no skill matches', async () => {
    const { view, box } = await openComposer()
    await view.user.type(box, '/nothing')
    const list = await screen.findByRole('listbox', { name: 'Skills' })
    await within(list).findByText('No skill matches “/nothing”.')
    await view.user.keyboard('{Enter}')
    await waitFor(() => expect(sentMessages().map((m) => m.text)).toEqual(['/nothing']))
  })

  it('lists an enabled package’s skills after the built-in ones', async () => {
    server.use(
      http.get('/api/v1/ai/plugin-packages', () =>
        HttpResponse.json([
          {
            name: 'scadbuddy',
            built_in: true,
            source: { kind: 'built_in', path: 'agent/plugins/scadbuddy' },
            review: { name: 'scadbuddy', description: null, version: null, skills: ['scadbuddy:print'], commands: [], agents: [], hooks: [], mcp_servers: [], files: [] },
            approved: true,
            enabled: true,
          },
          {
            name: 'greeter',
            source: { kind: 'git', url: 'https://git.example/greeter.git', ref: 'main', path: '' },
            commit_sha: 'a'.repeat(40),
            content_hash: 'sha256:x',
            review: { name: 'greeter', description: null, version: null, skills: ['greeter:hello'], commands: [], agents: [], hooks: [], mcp_servers: [], files: [] },
            approved: true,
            approved_at: null,
            allow_refused: false,
            enabled: true,
            pending: null,
            created_at: '2026-10-01T00:00:00Z',
            updated_at: '2026-10-01T00:00:00Z',
          },
        ]),
      ),
    )
    const { view, box } = await openComposer()
    await view.user.type(box, '/')
    const list = await screen.findByRole('listbox', { name: 'Skills' })
    await waitFor(() => expect(optionNames(list)).toEqual(['/scadbuddy:print', '/greeter:hello']))
  })

  it('says when the skills cannot be read', async () => {
    server.use(http.get('/api/v1/ai/plugin-packages', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    const { view, box } = await openComposer()
    await view.user.type(box, '/')
    const list = await screen.findByRole('listbox', { name: 'Skills' })
    expect(await within(list).findByText('The skills could not be read: down')).toBeInTheDocument()
    await view.user.keyboard('{Enter}')
    await waitFor(() => expect(sentMessages().map((m) => m.text)).toEqual(['/']))
  })
})
