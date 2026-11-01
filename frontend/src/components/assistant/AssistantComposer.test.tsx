import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'

// Plan Step 6.3 — every session is an AgentSession: the composer offers no session mode and
// sends none, a mode a tab of an earlier release remembered (`scadbuddy.assistant.mode`) is
// ignored, and the header shows no Durable badge.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

let agent: MockAgentTransport

const sentMessages = () =>
  agent.sent.filter((m): m is Extract<ClientMessage, { type: 'user.message' }> => m.type === 'user.message')

async function openComposer() {
  const view = renderPage(
    <Routes>
      <Route
        element={
          <AppShell
            embedded={false}
            assistantTransport={() => {
              agent = createMockAgentTransport({ stepMs: 0 })
              return agent
            }}
          />
        }
      >
        <Route path="*" element={<p>page</p>} />
      </Route>
    </Routes>,
    { route: '/' },
  )
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
  return { ...view, box }
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('the composer', () => {
  it('offers no session mode, sends none, and shows no Durable badge', async () => {
    const { user, box } = await openComposer()
    expect(screen.queryByRole('button', { name: /^Session mode/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Session mode' })).not.toBeInTheDocument()
    await user.type(box, 'hello{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).not.toHaveProperty('mode')
    const header = await screen.findByTestId('active-session-header')
    expect(within(header).queryByText('Durable')).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-mode-fallback')).not.toBeInTheDocument()
  })

  it('ignores a mode an earlier release remembered, and sends none', async () => {
    window.localStorage.setItem('scadbuddy.assistant.mode', 'classic')
    const { user, box } = await openComposer()
    expect(screen.queryByRole('button', { name: /^Session mode/ })).not.toBeInTheDocument()
    await user.type(box, 'hello{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).not.toHaveProperty('mode')
    // Left where it was: the composer neither reads nor clears it.
    expect(window.localStorage.getItem('scadbuddy.assistant.mode')).toBe('classic')
  })

  it('forks a chat from its header, and offers a fork from each reply (#2688)', async () => {
    const { user, box } = await openComposer()
    await user.type(box, 'hello{Enter}')
    const header = await screen.findByTestId('active-session-header')
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent(/idle|approval/i))
    await waitFor(() => expect(within(header).getByRole('button', { name: 'Fork' })).toBeEnabled())
    expect(screen.getAllByRole('button', { name: /Fork from here/ }).length).toBeGreaterThan(0)
    await user.click(within(header).getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(within(screen.getByTestId('active-session-header')).getByText(/Forked from/)).toBeInTheDocument())
  })
})
