import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import type { ClientMessage } from '../../agent/chat/protocol'
import { EXTERNAL_SESSION_ID, createMockAgentTransport, type MockAgentOptions, type MockAgentTransport } from '../../mocks/agent'
import { sessionWrites } from '../../mocks/features/assistantSessions'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'

// #792 — the panel's sessions: Fork and "Fork from here" (#794), and the session
// switcher (#795): forks nested under their parent, spend, last activity and status,
// rename and mark done. Design: docs/superpowers/specs/2026-10-09-session-switcher-design.md.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

const PARENT = 'Tune the gridfinity bin'
const PARENT_REPLY = 'Done: the bin is now 3 units (21 mm) tall.'

let agent: MockAgentTransport

function renderShell(options: MockAgentOptions = { stepMs: 0 }) {
  const factory = () => {
    agent = createMockAgentTransport(options)
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

const picker = () => screen.getByRole('navigation', { name: 'Sessions' })
const activeTitle = () => screen.getByTestId('active-session-title')

/** The panel open on the desktop agent's session, its reply shown. */
async function openParent() {
  const view = renderShell()
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  await view.user.click(await screen.findByRole('button', { name: /^Sessions/ }))
  await view.user.click(within(picker()).getByRole('button', { name: new RegExp(`^${PARENT}`) }))
  await screen.findByText(PARENT_REPLY)
  return view
}

/** The feed item holding `text` (FeedItemView's `data-feed-item`). */
function feedItem(text: string): HTMLElement {
  const item = within(screen.getByRole('log', { name: 'Conversation' })).getByText(text).closest<HTMLElement>('[data-feed-item]')
  if (!item) throw new Error(`no feed item holds ${text}`)
  return item
}

describe('Fork (#794)', () => {
  it('forks the open chat from its header, opens the fork, and links back to the parent', async () => {
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    expect(sessionWrites().at(-1)).toEqual({ method: 'POST', path: `/sessions/${EXTERNAL_SESSION_ID}/fork`, body: {} })
    await waitFor(() => expect(activeTitle()).toHaveTextContent(`${PARENT} (fork)`))
    expect(await screen.findByText(PARENT_REPLY)).toBeInTheDocument()
    // The fork is the user's: the composer is open, where the next message goes.
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveFocus()

    await user.click(screen.getByRole('button', { name: `Forked from ${PARENT}` }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(PARENT))
    expect(sentOf('session.attach').at(-1)).toEqual({ v: 1, type: 'session.attach', sessionId: EXTERNAL_SESSION_ID })
  })

  it('forks from a reply, up to it; from a user message, up to the reply before it, with that message to edit', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await user.type(box, 'Make the name bigger and send it{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Approve' }))
    await screen.findByText('Sent. Two copies are in the queue.')
    await user.type(box, 'make it blue{Enter}')
    await screen.findByText('Noted: "make it blue". Anything else on this model?')
    const parentTitle = activeTitle().textContent ?? ''

    // The first message has no reply before it to fork from.
    expect(within(feedItem('Make the name bigger and send it')).queryByRole('button', { name: 'Fork from here' })).toBeNull()

    await user.click(within(feedItem('Sent. Two copies are in the queue.')).getByRole('button', { name: 'Fork from here' }))
    const fromReply = sessionWrites().at(-1)
    expect(fromReply).toMatchObject({ method: 'POST', body: { up_to: expect.stringMatching(/^msg-/) } })
    await waitFor(() => expect(activeTitle()).toHaveTextContent(`${parentTitle} (fork)`))
    await screen.findByText('Sent. Two copies are in the queue.')
    const conversation = () => within(screen.getByRole('log', { name: 'Conversation' }))
    expect(conversation().queryByText('make it blue')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: `Forked from ${parentTitle}` }))
    await conversation().findByText('make it blue')
    await user.click(within(feedItem('make it blue')).getByRole('button', { name: 'Fork from here' }))
    expect(sessionWrites().at(-1)).toEqual({ ...fromReply })
    await waitFor(() => expect(conversation().queryByText('make it blue')).not.toBeInTheDocument())
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveValue('make it blue')
  })

  it('offers Fork only once the chat has a reply, saying why it cannot yet', async () => {
    const { user } = renderShell({ stepMs: 60_000 })
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), 'hello{Enter}')
    const fork = await screen.findByRole('button', { name: 'Fork' })
    expect(fork).toBeDisabled()
    expect(fork).toHaveAttribute('title', expect.stringMatching(/no reply to fork yet/i))
  })
})

describe('the session switcher (#795)', () => {
  it('nests a fork under its parent, with spend, last activity and status', async () => {
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(`${PARENT} (fork)`))
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))

    const parentRow = within(picker()).getByRole('button', { name: new RegExp(`^${PARENT}(?! \\(fork\\))`) }).closest('li')!
    const nested = within(parentRow).getByRole('list', { name: `Forks of ${PARENT}` })
    expect(within(nested).getByRole('button', { name: new RegExp(`^${PARENT} \\(fork\\)`) })).toBeInTheDocument()
    expect(within(parentRow).getByText('$0.02 of $1.00')).toBeInTheDocument()
    expect(within(nested).getByText('$0.00 of $1.00')).toBeInTheDocument()
    expect(within(parentRow).getAllByText(/ago|now/).length).toBeGreaterThan(0)
    expect(within(parentRow).getAllByText('Idle').length).toBeGreaterThan(0)
  })

  it("renames the user's chat and marks it done; an agent's chat offers neither", async () => {
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(`${PARENT} (fork)`))
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    expect(within(picker()).queryByRole('button', { name: `Rename ${PARENT}` })).toBeNull()
    expect(within(picker()).queryByRole('button', { name: `Mark ${PARENT} done` })).toBeNull()

    await user.click(within(picker()).getByRole('button', { name: `Rename ${PARENT} (fork)` }))
    const title = within(picker()).getByRole('textbox', { name: 'Chat title' })
    expect(title).toHaveFocus()
    await user.clear(title)
    await user.type(title, 'Bin, taller{Enter}')
    expect(sessionWrites().at(-1)).toMatchObject({ method: 'PATCH', body: { title: 'Bin, taller' } })
    expect(await within(picker()).findByRole('button', { name: /^Bin, taller/ })).toBeInTheDocument()
    expect(activeTitle()).toHaveTextContent('Bin, taller')

    await user.click(within(picker()).getByRole('button', { name: 'Mark Bin, taller done' }))
    expect(sessionWrites().at(-1)).toMatchObject({ method: 'PATCH', body: { done: true } })
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent('Done'))
    expect(within(picker()).queryByRole('button', { name: 'Mark Bin, taller done' })).toBeNull()
  })

  it('keeps a rename to edit when it is refused, and Escape leaves the title as it was', async () => {
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(`${PARENT} (fork)`))
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    await user.click(within(picker()).getByRole('button', { name: `Rename ${PARENT} (fork)` }))
    const title = within(picker()).getByRole('textbox', { name: 'Chat title' })
    await user.clear(title)
    await user.type(title, '   {Enter}')
    expect(await within(picker()).findByRole('alert')).toHaveTextContent(/title cannot be empty/i)
    await user.keyboard('{Escape}')
    expect(within(picker()).queryByRole('textbox', { name: 'Chat title' })).toBeNull()
    expect(within(picker()).getByRole('button', { name: new RegExp(`^${PARENT} \\(fork\\)`) })).toBeInTheDocument()
  })

  it('says a chat is out of budget', async () => {
    const { user } = renderShell({ stepMs: 0, budgetUsd: 0.01 })
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), 'Make the name bigger and send it{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Approve' }))
    await screen.findByText('Sent. Two copies are in the queue.')
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    const row = within(picker()).getByRole('button', { name: /^Make the name bigger/ }).closest('li')!
    expect(within(row).getByText('Out of budget')).toBeInTheDocument()
  })
})

describe('archive (#1885)', () => {
  const OWN = `${PARENT} (fork)`
  const composerBox = () => screen.getByRole('textbox', { name: 'Message the assistant' })
  const ownRow = () => within(picker()).queryByRole('button', { name: new RegExp(`^${OWN.replace(/[()]/g, '\\$&')}`) })

  /** The panel on a chat of the user's own, forked from the desktop agent's, with the picker open. */
  async function ownChat() {
    const view = await openParent()
    await view.user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(OWN))
    await view.user.click(screen.getByRole('button', { name: /^Sessions/ }))
    return view
  }

  it("archives the user's chat from the switcher: it leaves the list, and the Archived view lists it to unarchive", async () => {
    const { user } = await ownChat()
    // The agent's chat cannot be archived: it is not the user's.
    expect(within(picker()).queryByRole('button', { name: `Archive ${PARENT}` })).toBeNull()
    expect(screen.getByRole('button', { name: 'Sessions (2)' })).toBeInTheDocument()

    await user.click(within(picker()).getByRole('button', { name: `Archive ${OWN}` }))
    expect(sessionWrites().at(-1)).toEqual({ method: 'PATCH', path: expect.stringMatching(/^\/sessions\//), body: { archived: true } })
    await waitFor(() => expect(ownRow()).toBeNull())
    expect(screen.getByRole('button', { name: 'Sessions (1)' })).toBeInTheDocument()

    await user.click(within(picker()).getByRole('button', { name: 'Archived' }))
    expect(within(picker()).getByRole('button', { name: 'Archived' })).toHaveAttribute('aria-pressed', 'true')
    const archived = await within(picker()).findByRole('list', { name: 'Archived chats' })
    expect(within(archived).getByText(OWN)).toBeInTheDocument()
    expect(within(archived).queryByText(PARENT)).toBeNull()

    await user.click(within(archived).getByRole('button', { name: `Unarchive ${OWN}` }))
    expect(sessionWrites().at(-1)).toMatchObject({ method: 'PATCH', body: { archived: false } })
    expect(await within(picker()).findByText('No archived chats.')).toBeInTheDocument()
    await user.click(within(picker()).getByRole('button', { name: 'Chats' }))
    expect(within(picker()).getByRole('button', { name: `Archive ${OWN}` })).toBeInTheDocument()
  })

  it('opens an archived chat read-only: its transcript, the composer locked with a note, and Unarchive', async () => {
    const { user } = await ownChat()
    await user.click(within(picker()).getByRole('button', { name: `Archive ${OWN}` }))
    // The open chat stays open, read-only.
    await waitFor(() => expect(composerBox()).toBeDisabled())
    expect(screen.getByText(/This chat is archived/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'New chat' }))
    expect(composerBox()).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    await user.click(within(picker()).getByRole('button', { name: 'Archived' }))
    const archived = await within(picker()).findByRole('list', { name: 'Archived chats' })
    await user.click(within(archived).getByRole('button', { name: `Open ${OWN}` }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(OWN))
    expect(await screen.findByText(PARENT_REPLY)).toBeInTheDocument()
    expect(composerBox()).toBeDisabled()
    expect(composerBox()).toHaveAccessibleDescription(/archived.*read-only/i)
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    // Fork still continues it, as it does a done chat.
    expect(screen.getByRole('button', { name: 'Fork' })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: 'Unarchive' }))
    expect(sessionWrites().at(-1)).toMatchObject({ method: 'PATCH', body: { archived: false } })
    await waitFor(() => expect(composerBox()).toBeEnabled())
    expect(composerBox()).toHaveFocus()
    expect(screen.queryByText(/This chat is archived/)).toBeNull()
  })

  it('offers no Archive while a turn runs', async () => {
    const { user } = renderShell({ stepMs: 60_000 })
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), 'hello{Enter}')
    await screen.findByRole('button', { name: 'Stop' })
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    expect(within(picker()).queryByRole('button', { name: /^Archive / })).toBeNull()
  })
})
