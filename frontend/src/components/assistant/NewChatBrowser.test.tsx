import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'
import { PHONE_USER_AGENT } from '../../agent/chat/browserDraft'

// #7027 — a new chat's browser options: the user agent, viewport and locale its Playwright
// MCP reports, sent with the chat's first message only.

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

const browserToggle = () => screen.getByRole('button', { name: /^Browser/ })
const settings = () => screen.getByRole('group', { name: "This chat's browser" })

beforeEach(() => {
  window.localStorage.clear()
})

describe("a new chat's browser options", () => {
  it('sends no browser options when none are set', async () => {
    const { user, box } = await openComposer()
    expect(browserToggle()).toHaveAttribute('aria-expanded', 'false')
    await user.type(box, 'hello{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]).not.toHaveProperty('browser')
  })

  it('sends the Phone preset, and shows only for a new chat', async () => {
    const { user, box } = await openComposer()
    await user.click(browserToggle())
    await user.click(within(settings()).getByRole('button', { name: 'Phone' }))
    expect(within(settings()).getByRole('button', { name: 'Phone' })).toHaveAttribute('aria-pressed', 'true')
    expect(browserToggle()).toHaveTextContent('Phone')
    await user.type(box, 'hello{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]!.browser).toEqual({ user_agent: PHONE_USER_AGENT, viewport: { width: 390, height: 844 } })
    // Once the chat has started, its browser is fixed: the control is gone.
    await screen.findByTestId('active-session-header')
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Browser/ })).not.toBeInTheDocument())
  })

  it('sends exactly the values typed', async () => {
    const { user, box } = await openComposer()
    await user.click(browserToggle())
    const group = settings()
    await user.type(within(group).getByRole('textbox', { name: 'User agent' }), 'TestAgent/1.0')
    await user.type(within(group).getByRole('spinbutton', { name: 'Width' }), '1024')
    await user.type(within(group).getByRole('spinbutton', { name: 'Height' }), '768')
    await user.type(within(group).getByRole('textbox', { name: 'Locale' }), 'en-GB')
    await user.type(box, 'hello{Enter}')
    await waitFor(() => expect(sentMessages()).toHaveLength(1))
    expect(sentMessages()[0]!.browser).toEqual({ user_agent: 'TestAgent/1.0', viewport: { width: 1024, height: 768 }, locale: 'en-GB' })
  })

  it('holds the send while only one of width and height is set', async () => {
    const { user, box } = await openComposer()
    await user.click(browserToggle())
    await user.type(within(settings()).getByRole('spinbutton', { name: 'Width' }), '1024')
    expect(within(settings()).getByText('Set both width and height, or neither.')).toBeInTheDocument()
    await user.type(box, 'hello')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.click(within(settings()).getByRole('button', { name: 'Desktop (default)' }))
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('starts empty again for the next new chat', async () => {
    const { user, box } = await openComposer()
    await user.click(browserToggle())
    await user.click(within(settings()).getByRole('button', { name: 'Phone' }))
    await user.type(box, 'hello{Enter}')
    await screen.findByTestId('active-session-header')
    await user.click(screen.getByRole('button', { name: 'New chat' }))
    expect(browserToggle()).toHaveAccessibleName('Browser')
    await user.click(browserToggle())
    expect(within(settings()).getByRole('button', { name: 'Desktop (default)' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(settings()).getByRole('textbox', { name: 'User agent' })).toHaveValue('')
  })

  it("shows the agent's refusal and keeps the values to fix", async () => {
    const { user, box } = await openComposer()
    await user.click(browserToggle())
    await user.type(within(settings()).getByRole('spinbutton', { name: 'Width' }), '100')
    await user.type(within(settings()).getByRole('spinbutton', { name: 'Height' }), '844')
    await user.type(box, 'hello{Enter}')
    expect(await screen.findByText(/browser\.viewport\.width: Input should be greater than or equal to 320/)).toBeInTheDocument()
    expect(within(settings()).getByRole('spinbutton', { name: 'Width' })).toHaveValue(100)
    expect(screen.queryByTestId('active-session-header')).not.toBeInTheDocument()
  })
})
