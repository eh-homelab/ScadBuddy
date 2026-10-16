import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import type { ClientMessage, ServerEvent } from '../../agent/chat/protocol'
import { EXTERNAL_SESSION_ID, createMockAgentTransport, mockAgentSessions, type MockAgentOptions, type MockAgentTransport } from '../../mocks/agent'
import { sessionWrites } from '../../mocks/features/assistantSessions'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'

// #792 — the panel's sessions: Fork and "Fork from here" (#794), and the session
// switcher (#795): forks nested under their parent, spend, last activity and status,
// rename and mark done. Design: docs/superpowers/specs/2026-10-09-session-switcher-design.md.

vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => ({ available: true }) }))

const PARENT = 'Tune the gridfinity bin'
const PARENT_REPLY = 'Done: the bin is now 3 units (21 mm) tall.'

let agent: MockAgentTransport

/** `transport` is one to reuse, as an agent that outlives the tab does. */
function renderShell(options: MockAgentOptions = { stepMs: 0 }, transport?: MockAgentTransport) {
  const factory = () => {
    agent = transport ?? createMockAgentTransport(options)
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

type User = Awaited<ReturnType<typeof openParent>>['user']

/** #3673 — the row's actions menu (`Actions for <title>`), opened. */
async function rowMenu(user: User, title: string | RegExp) {
  await user.click(within(picker()).getByRole('button', { name: typeof title === 'string' ? `Actions for ${title}` : title }))
  return screen.findByRole('menu')
}

/** Picks one of a row's actions from its menu. */
async function rowAction(user: User, title: string, action: string) {
  await user.click(within(await rowMenu(user, title)).getByRole('menuitem', { name: action }))
}

/** Whether a row's menu offers an action; the menu is closed again. */
async function offers(user: User, title: string | RegExp, action: string | RegExp): Promise<boolean> {
  const found = within(await rowMenu(user, title)).queryByRole('menuitem', { name: action }) !== null
  await user.keyboard('{Escape}')
  await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  return found
}
const activeTitle = () => screen.getByTestId('active-session-title')

/** The panel open on the desktop agent's session, its reply shown. */
async function openParent() {
  const view = renderShell()
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  // The panel is a lazy chunk: on a cold transform cache the first test waits past the default.
  await view.user.click(await screen.findByRole('button', { name: /^Sessions/ }, { timeout: 10_000 }))
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

  // #2161: a failed fork's alert is the session's it failed on, not the panel's.
  it('says a fork failed on that chat only, not on the next one opened', async () => {
    server.use(
      http.post('/api/v1/ai/sessions/:id/fork', () => HttpResponse.json({ detail: 'the agent is busy' }, { status: 503 })),
    )
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The chat was not forked: the agent is busy')

    await user.click(screen.getByRole('button', { name: 'New chat' }))
    await user.type(screen.getByRole('textbox', { name: 'Message the assistant' }), 'hello{Enter}')
    await screen.findByText(/hello/, { selector: '[data-feed-item] *' })
    expect(screen.queryByText(/The chat was not forked/)).not.toBeInTheDocument()
  })

  // #2161: "Fork from here" on a user message never throws away what the user is writing.
  it('keeps a draft the user is writing when forking from a user message', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await user.type(box, 'Make the name bigger and send it{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Approve' }))
    await screen.findByText('Sent. Two copies are in the queue.')
    await user.type(box, 'make it blue{Enter}')
    await screen.findByText('Noted: "make it blue". Anything else on this model?')

    await user.type(box, 'half-written thought')
    await user.click(within(feedItem('make it blue')).getByRole('button', { name: 'Fork from here' }))
    const conversation = () => within(screen.getByRole('log', { name: 'Conversation' }))
    await waitFor(() => expect(conversation().queryByText('make it blue')).not.toBeInTheDocument())
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveValue('half-written thought')
  })

  it('offers Fork only once the chat has a reply, saying why it cannot yet', async () => {
    const { user } = renderShell({ stepMs: 60_000 })
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), 'hello{Enter}')
    const fork = await screen.findByRole('button', { name: 'Fork' })
    expect(fork).toBeDisabled()
    // Its hint is on the wrapper a disabled button needs to be hovered or focused (#3673).
    await user.hover(fork.parentElement!)
    expect(await screen.findByRole('tooltip')).toHaveTextContent(/no reply to fork yet/i)
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
    expect(within(picker()).queryByRole('button', { name: `Actions for ${PARENT}` })).toBeNull()

    await rowAction(user, `${PARENT} (fork)`, `Rename ${PARENT} (fork)`)
    const title = within(picker()).getByRole('textbox', { name: 'Chat title' })
    expect(title).toHaveFocus()
    await user.clear(title)
    await user.type(title, 'Bin, taller{Enter}')
    expect(sessionWrites().at(-1)).toMatchObject({ method: 'PATCH', body: { title: 'Bin, taller' } })
    expect(await within(picker()).findByRole('button', { name: /^Bin, taller/ })).toBeInTheDocument()
    expect(activeTitle()).toHaveTextContent('Bin, taller')

    await rowAction(user, 'Bin, taller', 'Mark Bin, taller done')
    expect(sessionWrites().at(-1)).toMatchObject({ method: 'PATCH', body: { done: true } })
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent('Done'))
    expect(await offers(user, 'Bin, taller', 'Mark Bin, taller done')).toBe(false)
  })

  it('keeps a rename to edit when it is refused, and Escape leaves the title as it was', async () => {
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(`${PARENT} (fork)`))
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    await rowAction(user, `${PARENT} (fork)`, `Rename ${PARENT} (fork)`)
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
    expect(within(picker()).queryByRole('button', { name: `Actions for ${PARENT}` })).toBeNull()
    expect(screen.getByRole('button', { name: 'Sessions (2)' })).toBeInTheDocument()

    await rowAction(user, OWN, `Archive ${OWN}`)
    expect(sessionWrites().at(-1)).toEqual({ method: 'PATCH', path: expect.stringMatching(/^\/sessions\//), body: { archived: true } })
    await waitFor(() => expect(ownRow()).toBeNull())
    expect(screen.getByRole('button', { name: 'Sessions (1)' })).toBeInTheDocument()

    await user.click(within(picker()).getByRole('tab', { name: 'Archived' }))
    expect(within(picker()).getByRole('tab', { name: 'Archived' })).toHaveAttribute('aria-selected', 'true')
    const archived = await within(picker()).findByRole('list', { name: 'Archived chats' })
    expect(within(archived).getByText(OWN)).toBeInTheDocument()
    expect(within(archived).queryByText(PARENT)).toBeNull()

    await user.click(within(archived).getByRole('button', { name: `Unarchive ${OWN}` }))
    expect(sessionWrites().at(-1)).toMatchObject({ method: 'PATCH', body: { archived: false } })
    expect(await within(picker()).findByText('No archived chats.')).toBeInTheDocument()
    await user.click(within(picker()).getByRole('tab', { name: 'Chats' }))
    expect(await offers(user, OWN, `Archive ${OWN}`)).toBe(true)
  })

  it('opens an archived chat read-only: its transcript, the composer locked with a note, and Unarchive', async () => {
    const { user } = await ownChat()
    await rowAction(user, OWN, `Archive ${OWN}`)
    // The open chat stays open, read-only.
    await waitFor(() => expect(composerBox()).toBeDisabled())
    expect(screen.getByText(/This chat is archived/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'New chat' }))
    expect(composerBox()).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    await user.click(within(picker()).getByRole('tab', { name: 'Archived' }))
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

  it("archives the open chat from its header, where the user's own chat offers it (#3738)", async () => {
    const { user } = await openParent()
    const header = () => screen.getByTestId('active-session-header')
    // The desktop agent's chat is not the user's to archive.
    expect(within(header()).queryByRole('button', { name: 'Archive' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(OWN))

    await user.click(within(header()).getByRole('button', { name: 'Archive' }))
    expect(sessionWrites().at(-1)).toEqual({ method: 'PATCH', path: expect.stringMatching(/^\/sessions\//), body: { archived: true } })
    await waitFor(() => expect(composerBox()).toBeDisabled())
    expect(screen.getByText(/This chat is archived/)).toBeInTheDocument()
    expect(within(header()).queryByRole('button', { name: 'Archive' })).toBeNull()
  })

  it('says why a header Archive was refused, and leaves the chat open to write in (#3864 review)', async () => {
    server.use(
      http.patch('/api/v1/ai/sessions/:id', () => HttpResponse.json({ detail: 'a turn is running' }, { status: 409 })),
    )
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(OWN))
    const header = screen.getByTestId('active-session-header')

    await user.click(within(header).getByRole('button', { name: 'Archive' }))
    expect(await within(header).findByRole('alert')).toHaveTextContent('The chat was not archived: a turn is running')
    expect(composerBox()).toBeEnabled()
    expect(within(header).getByRole('button', { name: 'Archive' })).toBeInTheDocument()
  })

  it('drops a chat unarchived elsewhere from a still-open Archived view (#2118)', async () => {
    const { user } = await ownChat()
    await rowAction(user, OWN, `Archive ${OWN}`)
    await waitFor(() => expect(composerBox()).toBeDisabled())
    await user.click(within(picker()).getByRole('tab', { name: 'Archived' }))
    const archived = await within(picker()).findByRole('list', { name: 'Archived chats' })
    expect(within(archived).getByText(OWN)).toBeInTheDocument()

    // Unarchived from the composer's note, with the Archived view still mounted.
    await user.click(screen.getByRole('button', { name: 'Unarchive' }))
    await waitFor(() => expect(composerBox()).toBeEnabled())
    expect(await within(picker()).findByText('No archived chats.')).toBeInTheDocument()
  })

  it('offers no Archive while a turn runs', async () => {
    const { user } = renderShell({ stepMs: 60_000 })
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), 'hello{Enter}')
    await screen.findByRole('button', { name: 'Stop' })
    expect(within(screen.getByTestId('active-session-header')).queryByRole('button', { name: 'Archive' })).toBeNull()
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    expect(await offers(user, 'hello', /^Archive /)).toBe(false)
  })
})

describe('handoff offers (#1284)', () => {
  const OWN = `${PARENT} (fork)`

  it("shows who the user's chat is offered to, and withdraws the offer", async () => {
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(OWN))
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))
    // The desktop agent's chat is offered too, but it is not the user's: its offer
    // shows, and there is nothing for the user to cancel there.
    mockAgentSessions()!.offerChat(PARENT, { kind: 'bearer', id: 'token:t2', label: 'MCP token:t2' })
    expect(await within(picker()).findByText('Offered to MCP token t2')).toBeInTheDocument()
    expect(within(picker()).queryByRole('button', { name: `Actions for ${PARENT}` })).toBeNull()

    mockAgentSessions()!.offerChat(OWN, { kind: 'bearer', id: 'token:t1', label: 'MCP token:t1' })
    expect(await within(picker()).findByText('Offered to MCP token t1')).toBeInTheDocument()

    await rowAction(user, OWN, `Cancel the offer of ${OWN}`)
    expect(sessionWrites().at(-1)).toEqual({ method: 'DELETE', path: expect.stringMatching(/^\/sessions\/.+\/handoff$/), body: {} })
    await waitFor(() => expect(within(picker()).queryByText('Offered to MCP token t1')).toBeNull())
    // The mock's answer names no title; the user's row is the one with a menu.
    expect(await offers(user, /^Actions for/, /^Cancel the offer of/)).toBe(false)
  })
  it('drops an offer once it lapses, with no new snapshot', async () => {
    const { user } = await openParent()
    await user.click(screen.getByRole('button', { name: 'Fork' }))
    await waitFor(() => expect(activeTitle()).toHaveTextContent(OWN))
    await user.click(screen.getByRole('button', { name: /^Sessions/ }))

    mockAgentSessions()!.offerChat(OWN, { kind: 'bearer', id: 'token:t1', label: 'MCP token:t1' }, 400)
    expect(await within(picker()).findByText('Offered to MCP token t1')).toBeInTheDocument()
    // The agent sends nothing when the offer lapses; the row stops showing it anyway.
    await waitFor(() => expect(within(picker()).queryByText('Offered to MCP token t1')).toBeNull(), { timeout: 3000 })
    expect(await offers(user, OWN, `Cancel the offer of ${OWN}`)).toBe(false)
  })
})

describe('a reload (#2372)', () => {
  it('reopens the panel this tab had open, on the chat it showed', async () => {
    const before = await openParent()
    before.unmount()

    renderShell()
    expect(await screen.findByRole('complementary', { name: 'Assistant' })).toBeVisible()
    expect(await screen.findByText(PARENT_REPLY)).toBeInTheDocument()
    expect(activeTitle()).toHaveTextContent(PARENT)
    expect(sentOf('session.attach').at(-1)).toEqual({ v: 1, type: 'session.attach', sessionId: EXTERNAL_SESSION_ID })
  })

  it('leaves a closed panel closed, and opens it on the chat it last showed', async () => {
    const before = await openParent()
    await before.user.click(screen.getByRole('button', { name: 'Close assistant' }))
    before.unmount()

    const { user } = renderShell()
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).toBeNull()
    await user.click(await screen.findByRole('button', { name: 'Assistant' }))
    expect(await screen.findByText(PARENT_REPLY)).toBeInTheDocument()
    expect(activeTitle()).toHaveTextContent(PARENT)
  })

  // #3249 review: the panel is a lazy chunk, so the shell has saved it as open before the
  // chat first renders; a new tab read that as a reload and jumped to the running chat.
  it('opens a new tab on a new chat, not on one of yours still running (#3164)', async () => {
    const shared = createMockAgentTransport({ stepMs: 60_000 })
    const first = renderShell(undefined, shared)
    await first.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await first.user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), 'still going{Enter}')
    await waitFor(() => expect(activeTitle()).toHaveTextContent('still going'))
    first.unmount()
    window.sessionStorage.clear()

    const { user } = renderShell(undefined, shared)
    await user.click(await screen.findByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: /^Sessions/ }))
    expect(await within(picker()).findByRole('button', { name: /^still going/ })).toBeInTheDocument()
    expect(screen.queryByTestId('active-session-title')).toBeNull()
    expect(sentOf('session.attach')).toEqual([])
  })

  it('forgets a remembered chat that is no longer listed (#2962 review)', async () => {
    window.sessionStorage.setItem('scadbuddy.assistant.open', '1')
    window.sessionStorage.setItem('scadbuddy.assistant.session', 'a-chat-since-deleted')

    renderShell()
    expect(await screen.findByRole('complementary', { name: 'Assistant' })).toBeVisible()
    // Forgotten, not swapped for another chat, and the panel opens on a blank one.
    await waitFor(() => expect(window.sessionStorage.getItem('scadbuddy.assistant.session')).toBeNull())
    expect(screen.queryByTestId('active-session-title')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveValue('')
  })

  it('forgets a remembered chat when the viewer has no chats left (#3616)', async () => {
    window.sessionStorage.setItem('scadbuddy.assistant.open', '1')
    window.sessionStorage.setItem('scadbuddy.assistant.session', 'a-chat-since-deleted')
    const inner = createMockAgentTransport({ stepMs: 0 })
    const noChats: MockAgentTransport = {
      ...inner,
      connect: (h) =>
        inner.connect({
          ...h,
          // The mock delivers parsed events, so the frame is a ServerEvent.
          onFrame: (frame) => {
            const event = frame as ServerEvent
            h.onFrame(event.type === 'sessions.snapshot' ? ({ ...event, sessions: [] } satisfies ServerEvent) : event)
          },
        }),
    }

    renderShell(undefined, noChats)
    expect(await screen.findByRole('complementary', { name: 'Assistant' })).toBeVisible()
    await waitFor(() => expect(window.sessionStorage.getItem('scadbuddy.assistant.session')).toBeNull())
  })
})

describe('the chat header (#2380)', () => {
  it('keeps the title a readable width: the badges and actions wrap under it rather than squeeze it', async () => {
    await openParent()
    // flex-1 alone is a 0% basis, so the row never wrapped and the title took the squeeze.
    expect(activeTitle()).toHaveClass('min-w-[min(12rem,100%)]')
    expect(activeTitle()).not.toHaveClass('min-w-0')
    expect(screen.getByTestId('active-session-header')).toHaveClass('flex-wrap')
  })
})
