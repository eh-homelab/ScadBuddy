import {
  panelWasOpen,
  reloaded,
  rememberPanelOpen,
  rememberSession,
  rememberedSession,
  sessionToRestore,
} from './panelMemory'

// #2372 — what a reloaded tab brings back.
describe('the assistant panel’s memory (#2372)', () => {
  const you = { kind: 'browser' } as const
  const sessions = {
    newest: { status: 'idle', origin: 'chat', owner: you },
    mcp: { status: 'running', origin: 'mcp', owner: you },
    waiting: { status: 'waiting_approval', origin: 'chat', owner: you },
    running: { status: 'running', origin: 'chat', owner: you },
  } as const
  const order = ['newest', 'mcp', 'waiting', 'running']

  it('restores the chat the tab showed while it is listed', () => {
    expect(sessionToRestore('newest', order, sessions)).toBe('newest')
  })

  it('else the newest chat still under way, never an MCP client’s session', () => {
    expect(sessionToRestore('gone', order, sessions)).toBe('waiting')
    expect(sessionToRestore(null, ['newest', 'mcp'], sessions)).toBeNull()
  })

  it('never falls back to another owner’s chat, however new (#3164)', () => {
    const theirs = { ...sessions, desktop: { status: 'running', origin: 'chat', owner: { kind: 'bearer' } } } as const
    expect(sessionToRestore(null, ['desktop', ...order], theirs)).toBe('waiting')
    expect(sessionToRestore(null, ['desktop'], theirs)).toBeNull()
  })

  it('takes a tab as reloaded only when it remembers a chat or an open panel (#3164)', () => {
    expect(reloaded()).toBe(false)
    rememberSession('s1')
    expect(reloaded()).toBe(true)
    rememberSession(null)
    rememberPanelOpen(true)
    expect(reloaded()).toBe(true)
  })

  it('remembers per tab, and forgets a closed panel and a new chat', () => {
    rememberPanelOpen(true)
    rememberSession('s1')
    expect([panelWasOpen(), rememberedSession()]).toEqual([true, 's1'])
    rememberPanelOpen(false)
    rememberSession(null)
    expect([panelWasOpen(), rememberedSession()]).toEqual([false, null])
  })
})
