import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'

// #790: the session header's spend meter and warning, and what a chat that used its
// budget offers, against the scripted agent (mocks/agent.ts) and the msw session routes
// (mocks/features/assistantSessions.ts). Every scripted turn costs $0.0184.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

let agent: MockAgentTransport
let budgetUsd = 1

function renderShell() {
  const factory = () => {
    agent = createMockAgentTransport({ stepMs: 0, budgetUsd })
    return agent
  }
  return renderPage(
    <Routes>
      <Route element={<AppShell embedded={false} assistantTransport={factory} />}>
        <Route path="*" element={<p>page</p>} />
      </Route>
    </Routes>,
    { route: '/m/name-keychain' },
  )
}

const sentOf = <T extends ClientMessage['type']>(type: T) =>
  agent.sent.filter((m): m is Extract<ClientMessage, { type: T }> => m.type === type)

/** Opens the panel, sends the first turn and approves its send, so one turn is paid for. */
async function firstTurn() {
  const view = renderShell()
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  await view.user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), 'Make it bigger{Enter}')
  await view.user.click(await screen.findByRole('button', { name: 'Approve' }))
  await screen.findByText('Sent. Two copies are in the queue.')
  return view
}

/** Spends the rest: a second turn at a $0.02 budget. */
async function spend() {
  budgetUsd = 0.02
  const view = await firstTurn()
  await view.user.type(screen.getByRole('textbox', { name: 'Message the assistant' }), 'and blue{Enter}')
  return { ...view, card: await screen.findByRole('region', { name: 'Chat budget' }) }
}

afterEach(() => {
  budgetUsd = 1
})

describe('the session budget in the assistant panel', () => {
  it('shows what the chat has spent of its budget, in cents', async () => {
    await firstTurn()
    const meter = await screen.findByTestId('session-budget')
    expect(meter).toHaveTextContent('$0.02 of $1.00')
    expect(within(meter).getByRole('meter', { name: 'Budget used' })).toHaveAttribute('aria-valuenow', '2')
    expect(screen.queryByText('This chat is close to its budget.')).not.toBeInTheDocument()
  })

  it('warns from 80% of the budget', async () => {
    budgetUsd = 0.02
    await firstTurn()
    // $0.0184 of $0.02 is 92%.
    expect(await screen.findByText('This chat is close to its budget.')).toBeInTheDocument()
    expect(screen.getByTestId('session-budget')).toHaveTextContent('$0.02 of $0.02')
  })

  it('says once that the chat used its budget, instead of the agent’s errors', async () => {
    const { card, user } = await spend()
    expect(card).toHaveTextContent('This chat used its $0.02 budget.')
    expect(screen.queryByText(/this chat used its \$0\.02 budget \(/)).not.toBeInTheDocument()
    expect(screen.queryByText('This chat is close to its budget.')).not.toBeInTheDocument()

    // Sending again is refused by the agent; still the one message, not a second error.
    await user.type(screen.getByRole('textbox', { name: 'Message the assistant' }), 'more{Enter}')
    await waitFor(() => expect(sentOf('user.message')).toHaveLength(3))
    expect(screen.getAllByRole('region', { name: 'Chat budget' })).toHaveLength(1)
    expect(screen.queryByText(/has spent its budget/)).not.toBeInTheDocument()
  })

  it('continues in a new chat: forks it and switches the panel to the fork', async () => {
    const { card, user } = await spend()
    await user.click(within(card).getByRole('button', { name: 'Continue in a new chat' }))
    await waitFor(() => expect(sentOf('session.attach')).toHaveLength(1))
    expect(sentOf('session.attach')[0]?.sessionId).not.toBe('chat-1')
    expect(await screen.findByTitle('Make it bigger (fork)')).toBeInTheDocument()
    // The transcript came with it, and a fresh budget.
    expect(screen.getByText('Make it bigger', { selector: 'p' })).toBeInTheDocument()
    expect(screen.getByTestId('session-budget')).toHaveTextContent('$0.00 of $0.02')
    expect(screen.queryByRole('region', { name: 'Chat budget' })).not.toBeInTheDocument()
  })

  it('raises this chat’s budget, user-only, and lets it go on', async () => {
    const { card, user } = await spend()
    const raise = within(card).getByRole('button', { name: 'Raise this chat’s budget' })
    expect(raise).toHaveAttribute('data-agent-user-only')
    await user.click(raise)
    const amount = within(card).getByRole('spinbutton', { name: 'Add (USD)' })
    expect(amount).toHaveValue(0.02)
    expect(amount.closest('form')).toHaveAttribute('data-agent-user-only')
    await user.clear(amount)
    await user.type(amount, '1')
    await user.click(within(card).getByRole('button', { name: 'Raise' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Chat budget' })).not.toBeInTheDocument())
    expect(screen.getByTestId('session-budget')).toHaveTextContent('$0.04 of $1.02')
  })

  it('starts a new chat', async () => {
    const { card, user } = await spend()
    await user.click(within(card).getByRole('button', { name: 'Start a new chat' }))
    expect(screen.queryByRole('region', { name: 'Chat budget' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-budget')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveFocus()
  })
})
