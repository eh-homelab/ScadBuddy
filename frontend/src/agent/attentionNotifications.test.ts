import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttentionNotice } from './attention'
import {
  NOTICE_BODY_MAX,
  attentionNotificationsState,
  noticeTitle,
  requestAttentionNotifications,
  useAttentionNotifications,
} from './attentionNotifications'

/** A stand-in for the browser's Notification, which jsdom lacks. */
class FakeNotification {
  static permission: NotificationPermission = 'granted'
  static requestPermission = vi.fn(async () => FakeNotification.permission)
  static made: FakeNotification[] = []
  onclick: (() => void) | null = null
  closed = false
  readonly title: string
  readonly options: NotificationOptions
  constructor(title: string, options: NotificationOptions) {
    this.title = title
    this.options = options
    FakeNotification.made.push(this)
  }
  close() {
    this.closed = true
  }
}

let visibility: DocumentVisibilityState = 'hidden'

beforeEach(() => {
  FakeNotification.permission = 'granted'
  FakeNotification.made = []
  FakeNotification.requestPermission.mockClear()
  vi.stubGlobal('Notification', FakeNotification)
  visibility = 'hidden'
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const tab: AttentionNotice = { id: 'question:q1', sessionId: 'sess-1', reason: 'tab_disconnected', message: 'The ScadBuddy tab closed; reopen it?' }
const blocked: AttentionNotice = { id: 'question:q2', sessionId: null, reason: 'blocked', message: 'Need a font' }

function setup(enabled = true) {
  const onOpen = vi.fn()
  const hook = renderHook(({ notices, on }: { notices: readonly AttentionNotice[] | null; on: boolean }) => useAttentionNotifications(notices, on, onOpen), {
    initialProps: { notices: null as readonly AttentionNotice[] | null, on: enabled },
  })
  return { ...hook, onOpen }
}

describe('useAttentionNotifications', () => {
  it('notifies an attention request that opens while the page is hidden, and a click opens its session', () => {
    const { rerender, onOpen } = setup()
    rerender({ notices: [], on: true })
    expect(FakeNotification.made).toHaveLength(0)
    rerender({ notices: [tab], on: true })
    expect(FakeNotification.made).toHaveLength(1)
    const n = FakeNotification.made[0]!
    expect(n.title).toBe('The assistant lost its ScadBuddy tab')
    expect(n.options).toEqual({ body: tab.message, tag: 'question:q1' })
    const focus = vi.spyOn(window, 'focus').mockImplementation(() => {})
    n.onclick?.()
    expect(focus).toHaveBeenCalled()
    expect(n.closed).toBe(true)
    expect(onOpen).toHaveBeenCalledWith('sess-1')
  })

  it('notifies each request once, and not again for one already announced', () => {
    const { rerender } = setup()
    rerender({ notices: [], on: true })
    rerender({ notices: [tab], on: true })
    rerender({ notices: [tab], on: true })
    rerender({ notices: [tab, blocked], on: true })
    expect(FakeNotification.made.map((n) => n.options.tag)).toEqual(['question:q1', 'question:q2'])
  })

  it('does not announce what was already waiting at the first read (a reload)', () => {
    const { rerender } = setup()
    rerender({ notices: [tab], on: true })
    expect(FakeNotification.made).toHaveLength(0)
    rerender({ notices: [tab, blocked], on: true })
    expect(FakeNotification.made.map((n) => n.options.tag)).toEqual(['question:q2'])
  })

  it('shows nothing while the page is visible, and does not announce that request later', () => {
    const { rerender } = setup()
    rerender({ notices: [], on: true })
    visibility = 'visible'
    rerender({ notices: [tab], on: true })
    visibility = 'hidden'
    rerender({ notices: [tab], on: true })
    expect(FakeNotification.made).toHaveLength(0)
  })

  it('shows nothing without the permission', () => {
    FakeNotification.permission = 'default'
    const { rerender } = setup()
    rerender({ notices: [], on: true })
    rerender({ notices: [tab], on: true })
    expect(FakeNotification.made).toHaveLength(0)
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled()
  })

  it('shows nothing when off (embedded, or the assistant gone)', () => {
    const { rerender } = setup(false)
    rerender({ notices: [], on: false })
    rerender({ notices: [tab], on: false })
    expect(FakeNotification.made).toHaveLength(0)
  })

  it('closes a notification once its request is answered, or the page is shown again', () => {
    const { rerender } = setup()
    rerender({ notices: [], on: true })
    rerender({ notices: [tab, blocked], on: true })
    const first = FakeNotification.made[0]!
    const second = FakeNotification.made[1]!
    rerender({ notices: [blocked], on: true })
    expect(first.closed).toBe(true)
    expect(second.closed).toBe(false)
    visibility = 'visible'
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(second.closed).toBe(true)
  })

  it('opens the panel without a session for a request the agent placed in none', () => {
    const { rerender, onOpen } = setup()
    rerender({ notices: [], on: true })
    rerender({ notices: [blocked], on: true })
    vi.spyOn(window, 'focus').mockImplementation(() => {})
    FakeNotification.made[0]!.onclick?.()
    expect(onOpen).toHaveBeenCalledWith(null)
  })

  it('cuts a long message to the body limit', () => {
    const { rerender } = setup()
    rerender({ notices: [], on: true })
    rerender({ notices: [{ ...blocked, message: 'x'.repeat(500) }], on: true })
    expect(FakeNotification.made[0]!.options.body).toHaveLength(NOTICE_BODY_MAX)
  })
})

describe('permission', () => {
  it('is unsupported without the API, and asks only when called', async () => {
    expect(attentionNotificationsState()).toBe('granted')
    FakeNotification.permission = 'default'
    expect(await requestAttentionNotifications()).toBe('default')
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
    expect(attentionNotificationsState()).toBe('unsupported')
    expect(await requestAttentionNotifications()).toBe('unsupported')
  })

  it('titles each reason', () => {
    expect(noticeTitle('question')).toBe('The assistant has a question')
    expect(noticeTitle('blocked')).toBe('The assistant is blocked')
    expect(noticeTitle('something-new')).toBe('The assistant needs you')
  })
})
