import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { createMockAgentTransport, type MockAgentOptions, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'
import { SESSION_MODE_KEY } from './AssistantChat'

// Plan 5d — the composer's session-mode picker, the header's Durable badge, and the
// note when a default durable session ran classic.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

let agent: MockAgentTransport
let options: MockAgentOptions = {}

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
              agent = createMockAgentTransport({ stepMs: 0, ...options })
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
  // The agent's default; the mock's own is classic.
  options = { defaultMode: 'durable' }
  window.localStorage.clear()
})

describe('session mode', () => {
  it('sends no mode by default, and shows the Durable badge on the durable session the default gave', async () => {
    const { user, box } = await openComposer()
    expect(screen.getByRole('combobox', { name: 'Session mode' })).toHaveValue('')
    await user.type(box, 'hello{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).not.toHaveProperty('mode')
    const header = await screen.findByTestId('active-session-header')
    expect(within(header).getByText('Durable')).toBeInTheDocument()
    // Its conversation is in the workflow: no fork.
    expect(within(header).getByRole('button', { name: 'Fork' })).toHaveAttribute('title', expect.stringMatching(/cannot be forked/))
    // Set at the start: the picker is gone once a session is open.
    expect(screen.queryByRole('combobox', { name: 'Session mode' })).not.toBeInTheDocument()
  })

  it('sends the picked mode, remembers it, and shows no badge on a classic session', async () => {
    const { user, box } = await openComposer()
    await user.click(screen.getByText('Session mode', { selector: 'summary' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Session mode' }), 'classic')
    expect(window.localStorage.getItem(SESSION_MODE_KEY)).toBe('classic')
    await user.type(box, 'hello{Enter}')
    await waitFor(() => expect(sentMessages()[0]).toMatchObject({ mode: 'classic' }))
    const header = await screen.findByTestId('active-session-header')
    expect(within(header).queryByText('Durable')).not.toBeInTheDocument()
  })

  it('starts from the remembered choice, and Default forgets it', async () => {
    window.localStorage.setItem(SESSION_MODE_KEY, 'durable')
    const { user } = await openComposer()
    const select = screen.getByRole('combobox', { name: 'Session mode' })
    expect(select).toHaveValue('durable')
    await user.selectOptions(select, '')
    expect(window.localStorage.getItem(SESSION_MODE_KEY)).toBeNull()
  })

  it('says when the default ran classic, and why', async () => {
    options = { defaultMode: 'durable', durableAvailable: false }
    const { user, box } = await openComposer()
    await user.type(box, 'hello{Enter}')
    const note = await screen.findByTestId('session-mode-fallback')
    expect(note).toHaveTextContent(/Running as Classic/)
    expect(note).toHaveTextContent(/no durable session worker/)
    expect(within(screen.getByTestId('active-session-header')).queryByText('Durable')).not.toBeInTheDocument()
  })

  it('shows the refusal when durable was asked for and cannot run', async () => {
    options = { defaultMode: 'durable', durableAvailable: false }
    window.localStorage.setItem(SESSION_MODE_KEY, 'durable')
    const { user, box } = await openComposer()
    await user.type(box, 'hello{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent(/durable/i)
  })
})
