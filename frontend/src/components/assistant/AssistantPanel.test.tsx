import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useRef, useState, type ReactNode } from 'react'
import { Link, Route, Routes } from 'react-router'
import { bridge } from '../../agent/bridge'
import type { ClientMessage } from '../../agent/chat/protocol'
import { useFullscreen } from '../../lib/useFullscreen'
import { EXTERNAL_SESSION_ID, createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { respondRequests, setPendingAnswers, setPendingApprovals } from '../../mocks/features/pendingInput'
import { setSessionResources } from '../../mocks/features/assistantSessions'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { HttpResponse, http } from 'msw'
import { AppShell } from '../AppShell'
import { Dialog } from '../ui/Dialog'
import { ResourceSessions } from './ResourceSessions'

/** What `useAiAvailability` answers; `set` re-renders whoever reads it, as the real one does. */
const availability = vi.hoisted(() => {
  type Value = { available: boolean; state?: string; chat?: 'refused' }
  let value: Value = { available: true }
  const listeners = new Set<() => void>()
  return {
    get: () => value,
    set: (next: Value) => {
      value = next
      for (const listener of listeners) listener()
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
})
vi.mock('../../agent/chat/availability', async () => {
  const { useSyncExternalStore } = await import('react')
  return { useAiAvailability: () => useSyncExternalStore(availability.subscribe, availability.get) }
})

let agent: MockAgentTransport
const factory = () => {
  agent = createMockAgentTransport({ stepMs: 0 })
  return agent
}

function renderShell(route = '/m/name-keychain', page: ReactNode = <p>page</p>) {
  return renderPage(
    <Routes>
      <Route element={<AppShell embedded={false} assistantTransport={factory} />}>
        <Route path="*" element={page} />
      </Route>
    </Routes>,
    { route },
  )
}

/** A page with a full-screen view, as the customizer has; jsdom takes the fallback. */
function FullscreenPage() {
  const view = useRef<HTMLDivElement>(null)
  const { mode, toggle } = useFullscreen(view)
  return (
    <div ref={view}>
      <button type="button" onClick={toggle}>
        {mode ? 'Exit full screen' : 'Full screen'}
      </button>
    </div>
  )
}

const pressShortcut = () => fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })

const sentOf = <T extends ClientMessage['type']>(type: T) =>
  agent.sent.filter((m): m is Extract<ClientMessage, { type: T }> => m.type === type)

async function openAndSend(text = 'Make the name bigger and send it') {
  const view = renderShell()
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
  await view.user.type(box, `${text}{Enter}`)
  await screen.findByRole('region', { name: 'Needs your approval' })
  return view
}

afterEach(() => {
  availability.set({ available: true })
})

describe('assistant panel', () => {
  it('is hidden, shortcut included, when AI is off', async () => {
    availability.set({ available: false, state: 'not_configured' })
    renderShell()
    expect(screen.queryByRole('button', { name: 'Assistant' })).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    await act(async () => {})
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
  })

  it('keeps an open panel through an outage, and drops it only when the agent says AI is off', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await user.type(box, 'Make the name bigger{Enter}')
    const transcript = (await screen.findAllByText('Make the name bigger')).length

    // The agent is restarting: its status read fails, and the panel stays, transcript and all.
    act(() => availability.set({ available: false, state: 'unreachable' }))
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()
    expect(screen.getAllByText('Make the name bigger')).toHaveLength(transcript)
    expect(screen.getByRole('button', { name: 'Assistant' })).toBeInTheDocument()
    act(() => availability.set({ available: false, state: 'unavailable' }))
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()

    // Its chat gate refuses this address: off for this page.
    act(() => availability.set({ available: false, state: 'unavailable', chat: 'refused' }))
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Assistant' })).not.toBeInTheDocument()

    // Back, then switched off (no credential): the same.
    act(() => availability.set({ available: true, state: 'configured' }))
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    expect(await screen.findByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()
    act(() => availability.set({ available: false, state: 'not_configured' }))
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
  })

  it('opens with Ctrl+` and focuses the composer; Escape closes it and returns focus', async () => {
    const { user } = renderShell()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await waitFor(() => expect(box).toHaveFocus())
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Assistant' })).toHaveFocus()
  })

  it('comes out of full screen for Ctrl+` rather than opening out of sight', async () => {
    const { user } = renderShell('/m/name-keychain', <FullscreenPage />)
    await user.click(screen.getByRole('button', { name: 'Full screen' }))

    pressShortcut()
    expect(await screen.findByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await waitFor(() => expect(box).toHaveFocus())
  })

  it('brings back a panel full screen had hidden, rather than closing it unseen', async () => {
    const { user } = renderShell('/m/name-keychain', <FullscreenPage />)
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await screen.findByRole('textbox', { name: 'Message the assistant' })
    await user.click(screen.getByRole('button', { name: 'Full screen' }))

    pressShortcut()
    expect(await screen.findByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveFocus(),
    )
  })

  it('offers prompts for the page and sends the page context with the turn', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: 'Make it fit the A1 mini plate' }))
    await screen.findByText('Make it fit the A1 mini plate', { selector: 'p' })
    expect(sentOf('user.message')[0]).toMatchObject({
      text: 'Make it fit the A1 mini plate',
      context: { route: '/m/name-keychain', modelSlug: 'name-keychain' },
    })
    expect(sentOf('user.message')[0]).not.toHaveProperty('sessionId')
    // #254: the browser bridge's view rides along; the shell's tools are live everywhere.
    expect(sentOf('user.message')[0]?.context.tools).toEqual(expect.arrayContaining(['navigate', 'snapshot']))
    expect(sentOf('user.message')[0]?.context.dialogs).toEqual([])
  })

  it('does not let the bridge fallbacks speak for the user (#254)', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await screen.findByRole('textbox', { name: 'Message the assistant' })
    const typed = await bridge.call('fill', { label: 'Message the assistant', value: 'send it' })
    expect(!typed.ok && typed.error.code).toBe('refused')
    const prompt = await bridge.call('click', { role: 'button', name: 'Make it fit the A1 mini plate' })
    expect(!prompt.ok && prompt.error.code).toBe('refused')
    expect(sentOf('user.message')).toEqual([])
  })

  it('streams text, shows the tool call with its risk, sources and version', async () => {
    await openAndSend()
    const log = screen.getByRole('log', { name: 'Conversation' })
    expect(within(log).getByText(/make the/)).toBeInTheDocument()
    expect(within(log).getByText('name', { selector: 'strong' })).toBeInTheDocument()
    const [write, send] = screen.getAllByTestId('agent-tool')
    expect(within(write!).getByText('set_parameters')).toBeInTheDocument()
    expect(within(write!).getByText('write')).toBeInTheDocument()
    expect(within(write!).getByText('Why? 2 sources')).toBeInTheDocument()
    expect(within(write!).getByRole('link', { name: 'OpenSCAD customizer parameters' })).toHaveAttribute(
      'href',
      'https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Customizer',
    )
    expect(within(write!).getByRole('link', { name: /Undo from version a1b2c3d/ })).toHaveAttribute(
      'href',
      '/m/name-keychain/versions',
    )
    expect(within(send!).getByText('outward')).toBeInTheDocument()
    expect(within(send!).getByText('running…')).toBeInTheDocument()
  })

  it('shows tool arguments only in Advanced, and remembers the switch per browser', async () => {
    window.localStorage.removeItem('scadbuddy.assistant.advanced')
    const { user } = await openAndSend()
    expect(screen.queryAllByTestId('agent-tool-arguments')).toEqual([])
    const advanced = screen.getByRole('button', { name: 'Advanced' })
    expect(advanced).toHaveAttribute('aria-pressed', 'false')
    await user.click(advanced)
    expect(advanced).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getAllByTestId('agent-tool-arguments').length).toBeGreaterThan(0)
    expect(window.localStorage.getItem('scadbuddy.assistant.advanced')).toBe('1')
    window.localStorage.removeItem('scadbuddy.assistant.advanced')
  })

  it('says on an empty chat what Advanced will show, so the switch visibly takes effect (#1488)', async () => {
    window.localStorage.removeItem('scadbuddy.assistant.advanced')
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: 'New chat' }))
    const note = /Advanced: tool arguments, sources and memory details will be shown/
    expect(screen.queryByText(note)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(screen.getByText(note)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(screen.queryByText(note)).not.toBeInTheDocument()
    window.localStorage.removeItem('scadbuddy.assistant.advanced')
  })

  it('holds the outward step until Approve, then sends the decision', async () => {
    const { user } = await openAndSend()
    const card = screen.getByRole('region', { name: 'Needs your approval' })
    expect(card).toHaveTextContent('Send name-keychain to Bambuddy project "Keychains", 2 copies?')
    const approve = within(card).getByRole('button', { name: 'Approve' })
    expect(approve).toHaveAttribute('data-agent-user-only')
    expect(within(card).getByRole('button', { name: 'Deny' })).toHaveAttribute('data-agent-user-only')

    // Nothing proceeds without the decision.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    expect(respondRequests()).toEqual([])
    expect(screen.queryByText('Queued 2 copies in the Keychains project.')).not.toBeInTheDocument()
    expect(screen.getByTestId('agent-status')).toHaveTextContent('Waiting for approval')

    await user.click(approve)
    // Through the one respond route (#815), not the socket.
    expect(respondRequests()).toEqual([{ id: expect.stringMatching(/^approval:/), body: { kind: 'approval', decision: 'approve' } }])
    expect(sentOf('approval.decision')).toEqual([])
    await screen.findByText('Queued 2 copies in the Keychains project.')
    await screen.findByText('Sent. Two copies are in the queue.')
    expect(within(card).getByText('Approved by You.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent('Idle'))
  })

  it('Deny sends a refusal and nothing is sent', async () => {
    const { user } = await openAndSend()
    await user.click(screen.getByRole('button', { name: 'Deny' }))
    expect(respondRequests()).toMatchObject([{ body: { kind: 'approval', decision: 'deny' } }])
    await screen.findByText('Denied: nothing was sent.')
    expect(screen.queryByText('Queued 2 copies in the Keychains project.')).not.toBeInTheDocument()
  })

  it('shows waiting approvals on the header button with the panel closed, and in the tab title (#815)', async () => {
    setPendingApprovals(2)
    document.title = 'ScadBuddy'
    const { user } = renderShell()
    const button = await screen.findByRole('button', { name: 'Assistant, 2 waiting for you' })
    expect(button).toHaveAttribute('title', 'Assistant (Ctrl+`): 2 waiting for you (2 approvals)')
    expect(within(button).getByTestId('assistant-attention')).toHaveTextContent('2')
    // Announced, not only shown: the badge appears while focus is elsewhere.
    const live = screen.getByTestId('assistant-attention-live')
    expect(live).toHaveAttribute('aria-live', 'polite')
    expect(live).toHaveTextContent('2 waiting for you')
    await waitFor(() => expect(document.title).toBe('(2) ScadBuddy'))

    // Decided elsewhere: toggling the panel reads again, and the badge goes.
    setPendingApprovals(0)
    await user.click(button)
    const plain = await screen.findByRole('button', { name: 'Assistant' })
    expect(within(plain).queryByTestId('assistant-attention')).not.toBeInTheDocument()
    expect(screen.getByTestId('assistant-attention-live')).toBeEmptyDOMElement()
    expect(plain).toHaveAttribute('title', 'Assistant (Ctrl+`)')
    await waitFor(() => expect(document.title).toBe('ScadBuddy'))
  })

  it('shows a done summary beside the badge, not in the waiting count or the tab title (#815)', async () => {
    setPendingAnswers(1, 0, 1)
    document.title = 'ScadBuddy'
    renderShell()
    const button = await screen.findByRole('button', { name: 'Assistant, 1 waiting for you, 1 summary' })
    expect(button).toHaveAttribute('title', 'Assistant (Ctrl+`): 1 waiting for you (1 question), 1 summary')
    expect(within(button).getByTestId('assistant-attention')).toHaveTextContent('1')
    expect(within(button).getByTestId('assistant-summaries')).toHaveTextContent('1 summary')
    expect(screen.getByTestId('assistant-attention-live')).toHaveTextContent('1 waiting for you')
    await waitFor(() => expect(document.title).toBe('(1) ScadBuddy'))
  })

  it('a done summary alone waits for nothing: no count, no title prefix, but still shown (#815)', async () => {
    setPendingAnswers(0, 0, 1)
    document.title = 'ScadBuddy'
    renderShell()
    const button = await screen.findByRole('button', { name: 'Assistant, 1 summary' })
    expect(within(button).queryByTestId('assistant-attention')).not.toBeInTheDocument()
    expect(within(button).getByTestId('assistant-summaries')).toHaveTextContent('1 summary')
    expect(screen.getByTestId('assistant-attention-live')).toBeEmptyDOMElement()
    expect(document.title).toBe('ScadBuddy')
  })

  it('shows the badge embedded in Bambuddy, but leaves the frame\'s unseen title alone (#815)', async () => {
    setPendingApprovals(1)
    document.title = 'ScadBuddy'
    renderPage(
      <Routes>
        <Route element={<AppShell embedded assistantTransport={factory} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
      { route: '/' },
    )
    const button = await screen.findByRole('button', { name: 'Assistant, 1 waiting for you' })
    expect(within(button).getByTestId('assistant-attention')).toHaveTextContent('1')
    expect(document.title).toBe('ScadBuddy')
  })

  it('Stop interrupts the turn', async () => {
    const { user } = await openAndSend()
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(sentOf('session.interrupt')).toEqual([{ v: 1, type: 'session.interrupt', sessionId: 'chat-1' }])
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent('Idle'))
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()
  })

  it('lists sessions with origin and owner, and takes over one an agent controls', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    const picker = screen.getByRole('navigation', { name: 'Sessions' })
    expect(within(picker).getByText('MCP')).toBeInTheDocument()
    expect(within(picker).getByText('Claude Desktop')).toBeInTheDocument()
    await user.click(within(picker).getByRole('button', { name: /Tune the gridfinity bin/ }))
    expect(sentOf('session.attach')).toEqual([{ v: 1, type: 'session.attach', sessionId: EXTERNAL_SESSION_ID }])

    // Replayed transcript; the composer is locked while someone else holds it.
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')
    expect(screen.getByText('Controlled by Claude Desktop')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toBeDisabled()

    const takeOver = screen.getByRole('button', { name: 'Take over' })
    expect(takeOver).toHaveAttribute('data-agent-user-only')
    await user.click(takeOver)
    expect(sentOf('session.handoff')).toEqual([{ v: 1, type: 'session.handoff', sessionId: EXTERNAL_SESSION_ID }])
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toBeEnabled())
    expect(screen.queryByText('Controlled by Claude Desktop')).not.toBeInTheDocument()
  })

  it("opens a session that changed the page's model in the panel, closed or already open (#931)", async () => {
    const { user } = renderShell('/m/gridfinity-bin', <ResourceSessions resource={{ type: 'model', id: 'gridfinity-bin' }} />)
    const panel = () => screen.queryByRole('complementary', { name: 'Assistant' })
    expect(panel()).not.toBeInTheDocument()

    await user.click(await screen.findByRole('button', { name: 'Changed by assistant (1)' }))
    await user.click(screen.getByRole('button', { name: /Tune the gridfinity bin.*·/ }))
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')
    expect(panel()).toBeVisible()
    expect(sentOf('session.attach')).toEqual([{ v: 1, type: 'session.attach', sessionId: EXTERNAL_SESSION_ID }])

    // Open and on another chat: picking it again switches back to it.
    await user.click(screen.getByRole('button', { name: 'New chat' }))
    await waitFor(() => expect(screen.queryByText('Done: the bin is now 3 units (21 mm) tall.')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Changed by assistant (1)' }))
    await user.click(screen.getByRole('button', { name: /Tune the gridfinity bin.*·/ }))
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')
    expect(sentOf('session.attach')).toHaveLength(2)
  })

  it('selects a requested session once: a remounted panel does not open it again (#931)', async () => {
    const { user } = renderShell('/m/gridfinity-bin', <ResourceSessions resource={{ type: 'model', id: 'gridfinity-bin' }} />)
    await user.click(await screen.findByRole('button', { name: 'Changed by assistant (1)' }))
    await user.click(screen.getByRole('button', { name: /Tune the gridfinity bin.*·/ }))
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')

    // The assistant goes off (the panel unmounts) and comes back: a fresh panel, on nothing.
    act(() => availability.set({ available: false, state: 'not_configured' }))
    await waitFor(() => expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument())
    act(() => availability.set({ available: true }))
    await user.click(await screen.findByRole('button', { name: 'Assistant' }))
    await screen.findByRole('textbox', { name: 'Message the assistant' })
    await waitFor(() => expect(agent.sent.some((m) => m.type === 'tab.bind')).toBe(true))
    expect(sentOf('session.attach')).toEqual([])
    expect(screen.queryByText('Done: the bin is now 3 units (21 mm) tall.')).not.toBeInTheDocument()
  })

  it('drops a requested session the panel never got to when the assistant goes off (#931)', async () => {
    const { user } = renderShell('/m/gridfinity-bin', <ResourceSessions resource={{ type: 'model', id: 'gridfinity-bin' }} />)
    await user.click(await screen.findByRole('button', { name: 'Changed by assistant (1)' }))
    const item = screen.getByRole('button', { name: /Tune the gridfinity bin.*·/ })
    // Picked, and the assistant goes off before the panel has loaded.
    act(() => {
      fireEvent.click(item)
      availability.set({ available: false, state: 'not_configured' })
    })
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()

    act(() => availability.set({ available: true }))
    await user.click(await screen.findByRole('button', { name: 'Assistant' }))
    await screen.findByRole('textbox', { name: 'Message the assistant' })
    await waitFor(() => expect(agent.sent.some((m) => m.type === 'tab.bind')).toBe(true))
    expect(sentOf('session.attach')).toEqual([])
  })

  it("filters the session picker to the sessions that changed this page's model (#931)", async () => {
    const view = renderShell('/m/gridfinity-bin')
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await view.user.type(box, 'Make the name bigger and send it{Enter}')
    await screen.findByRole('region', { name: 'Needs your approval' })

    await view.user.click(screen.getByRole('button', { name: 'Sessions (2)' }))
    const picker = screen.getByRole('navigation', { name: 'Sessions' })
    expect(within(picker).getAllByRole('listitem')).toHaveLength(2)
    const only = within(picker).getByRole('checkbox', { name: 'Only sessions that changed gridfinity-bin' })
    expect(only).not.toBeChecked()
    await view.user.click(only)
    // The desktop agent's session changed gridfinity-bin; the new chat changed nothing.
    await waitFor(() => expect(within(picker).getAllByRole('listitem')).toHaveLength(1))
    expect(within(picker).getByRole('button', { name: /Tune the gridfinity bin/ })).toBeInTheDocument()

    await view.user.click(only)
    expect(within(picker).getAllByRole('listitem')).toHaveLength(2)
  })

  it('says when no session changed the model, and when the agent cannot say (#931)', async () => {
    const view = renderShell('/m/name-keychain')
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    const picker = screen.getByRole('navigation', { name: 'Sessions' })
    await view.user.click(within(picker).getByRole('checkbox', { name: 'Only sessions that changed name-keychain' }))
    expect(await within(picker).findByText('No session changed name-keychain.')).toBeInTheDocument()

    server.use(
      http.get('/api/v1/ai/resources/:type/:id/sessions', () =>
        HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 }),
      ),
    )
    await view.user.click(within(picker).getByRole('checkbox'))
    await view.user.click(within(picker).getByRole('checkbox'))
    const alert = await within(picker).findByRole('alert')
    expect(alert).toHaveTextContent('the AI database is unreachable')
    // The list under it is not filtered, and says so.
    expect(alert).toHaveTextContent('Showing every session')
    // The full list stays usable.
    expect(within(picker).getByRole('button', { name: /Tune the gridfinity bin/ })).toBeInTheDocument()
  })

  it("doesn't say no session changed the model when the ones that did aren't loaded here (#931)", async () => {
    server.use(
      http.get('/api/v1/ai/resources/:type/:id/sessions', () => HttpResponse.json({ sessions: [{ id: 'older-session' }] })),
    )
    const view = renderShell('/m/name-keychain')
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    const picker = screen.getByRole('navigation', { name: 'Sessions' })
    await view.user.click(within(picker).getByRole('checkbox'))
    expect(await within(picker).findByText('None of the loaded sessions changed name-keychain.')).toBeInTheDocument()
    expect(within(picker).queryByText('No session changed name-keychain.')).not.toBeInTheDocument()
  })

  it('shows a loading state, not the full list, while the model filter loads (#931)', async () => {
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.get('/api/v1/ai/resources/:type/:id/sessions', async () => {
        await held
        return HttpResponse.json({ sessions: [{ id: EXTERNAL_SESSION_ID }] })
      }),
    )
    const view = renderShell('/m/gridfinity-bin')
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    const picker = screen.getByRole('navigation', { name: 'Sessions' })
    await view.user.click(within(picker).getByRole('checkbox'))
    expect(await within(picker).findByText('Finding the sessions that changed gridfinity-bin…')).toBeInTheDocument()
    expect(within(picker).queryByRole('listitem')).not.toBeInTheDocument()
    act(() => release())
    expect(await within(picker).findByRole('button', { name: /Tune the gridfinity bin/ })).toBeInTheDocument()
  })

  it('says when the model filter reached the most sessions it reads (#931)', async () => {
    server.use(
      http.get('/api/v1/ai/resources/:type/:id/sessions', ({ request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'))
        return HttpResponse.json({
          sessions: [{ id: EXTERNAL_SESSION_ID }, ...Array.from({ length: limit - 1 }, (_, i) => ({ id: `old-${i}` }))],
        })
      }),
    )
    const view = renderShell('/m/gridfinity-bin')
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    const picker = screen.getByRole('navigation', { name: 'Sessions' })
    await view.user.click(within(picker).getByRole('checkbox'))
    expect(await within(picker).findByText(/the 500 most recently updated/)).toBeInTheDocument()
  })

  it("does not carry the model filter to another model's page (#931)", async () => {
    const view = renderShell('/m/gridfinity-bin', <Link to="/m/name-keychain">other model</Link>)
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    await view.user.click(screen.getByRole('checkbox', { name: 'Only sessions that changed gridfinity-bin' }))
    await view.user.click(screen.getByRole('link', { name: 'other model' }))
    expect(await screen.findByRole('checkbox', { name: 'Only sessions that changed name-keychain' })).not.toBeChecked()
  })

  it('offers no model filter off a model page (#931)', async () => {
    const view = renderShell('/')
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    expect(within(screen.getByRole('navigation', { name: 'Sessions' })).queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it("shows what a session touched, linking to each resource's page (#931)", async () => {
    const { user } = renderShell('/')
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    await user.click(screen.getByRole('button', { name: /Tune the gridfinity bin/ }))
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')

    const touched = screen.getByRole('button', { name: 'Touched' })
    expect(touched).toHaveAttribute('aria-expanded', 'false')
    await user.click(touched)
    expect(touched).toHaveAttribute('aria-expanded', 'true')
    const panel = screen.getByRole('region', { name: 'What this session touched' })
    const revisions = await within(panel).findByRole('group', { name: 'Revisions' })
    expect(within(revisions).getByRole('link', { name: /3f9c2a1/ })).toHaveAttribute(
      'href',
      '/m/gridfinity-bin?version=3f9c2a1b7d4e',
    )

    await user.click(touched)
    expect(screen.queryByRole('region', { name: 'What this session touched' })).not.toBeInTheDocument()
  })

  it('reads Touched again when a tool call finishes, without waiting for the turn (#931)', async () => {
    let inject: (frame: unknown) => void = () => {}
    const spying = () => {
      const inner = factory()
      return {
        ...inner,
        connect: (h: Parameters<typeof inner.connect>[0]) => {
          inject = (frame) => act(() => h.onFrame(frame as never))
          inner.connect(h)
        },
      }
    }
    const view = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} assistantTransport={spying} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
      { route: '/' },
    )
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    await view.user.click(screen.getByRole('button', { name: /Tune the gridfinity bin/ }))
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')
    await view.user.click(screen.getByRole('button', { name: 'Touched' }))
    const panel = screen.getByRole('region', { name: 'What this session touched' })
    await within(panel).findByRole('group', { name: 'Presets' })

    setSessionResources(EXTERNAL_SESSION_ID, [
      { type: 'output', id: 'out-mid', action: 'created', model: 'gridfinity-bin', before: null, after: null, tool: 'save_output', at: '2026-10-03T09:01:00.000Z' },
    ])
    const status = screen.getByTestId('agent-status').textContent
    const base = { v: 1, sessionId: EXTERNAL_SESSION_ID }
    inject({ ...base, type: 'tool.call', id: 'tool-ext-2', name: 'mcp__scadbuddy__save_output', input: { slug: 'gridfinity-bin' }, risk: 'write' })
    inject({ ...base, type: 'tool.result', id: 'tool-ext-2', ok: true, summary: 'Saved.' })

    expect(await within(panel).findByRole('link', { name: /out-mid/ })).toHaveAttribute('href', '/edit/out-mid')
    // Read on the tool result alone: the session's status never moved.
    expect(screen.getByTestId('agent-status').textContent).toBe(status)
  })

  it('reports a malformed frame instead of rendering it', async () => {
    const bad = () => ({
      connect: ({ onFrame }: { onFrame: (f: unknown) => void }) => {
        queueMicrotask(() => onFrame({ v: 1, type: 'approval.required', sessionId: 's', id: 'a', tool: 't', summary: 'x', risk: 'write' }))
      },
      send: () => 'sent' as const,
      close: () => {},
    })
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} assistantTransport={bad} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
    )
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    expect(await screen.findByText(/Ignored a malformed message/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })
})

/** A page with a dialog, as the customizer's Print is. */
function DialogPage({ startOpen = false }: { startOpen?: boolean }) {
  const [open, setOpen] = useState(startOpen)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Print…
      </button>
      <Dialog open={open} title="Print" onClose={() => setOpen(false)} footer={<button type="button">Print it</button>}>
        <input aria-label="Copies" />
      </Dialog>
    </>
  )
}

describe('the assistant beside a dialog (#798)', () => {
  async function openBoth() {
    const view = renderShell('/m/name-keychain', <DialogPage />)
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    const composer = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await view.user.click(screen.getByRole('button', { name: 'Print…' }))
    const dialog = screen.getByRole('dialog', { name: 'Print' })
    return { ...view, composer, dialog }
  }

  it('lets the chat be typed in while the dialog stays open', async () => {
    const { user, composer, dialog } = await openBoth()
    expect(within(dialog).getByRole('textbox', { name: 'Copies' })).toHaveFocus()

    await user.click(composer)
    await user.keyboard('Which spool is the grey one?')
    expect(composer).toHaveValue('Which spool is the grey one?')
    expect(dialog).toBeInTheDocument()
  })

  it('is not hidden from screen readers by the dialog while it is beside it', async () => {
    const { user, dialog } = await openBoth()
    expect(dialog).not.toHaveAttribute('aria-modal', 'true')

    // Without the chat beside it, the dialog is modal again.
    await user.click(screen.getByRole('button', { name: 'Close assistant' }))
    await waitFor(() => expect(dialog).toHaveAttribute('aria-modal', 'true'))
  })

  it('does not close the dialog for an Escape pressed in the chat', async () => {
    const { user, composer } = await openBoth()
    await user.click(composer)
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('moves between the dialog and the chat with Ctrl+`', async () => {
    const { composer, dialog } = await openBoth()
    const copies = within(dialog).getByRole('textbox', { name: 'Copies' })
    expect(copies).toHaveFocus()

    pressShortcut()
    await waitFor(() => expect(composer).toHaveFocus())
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeVisible()
    pressShortcut()
    await waitFor(() => expect(copies).toHaveFocus())
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeVisible()
  })

  it('leaves focus in the chat when a dialog opens while the user is typing there', async () => {
    const view = renderShell('/m/name-keychain', <DialogPage />)
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    const composer = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await waitFor(() => expect(composer).toHaveFocus())
    // As the agent's open_print_dialog does: the dialog opens without the user's click.
    fireEvent.click(screen.getByRole('button', { name: 'Print…' }))
    expect(screen.getByRole('dialog', { name: 'Print' })).toBeInTheDocument()
    expect(composer).toHaveFocus()
  })
})
