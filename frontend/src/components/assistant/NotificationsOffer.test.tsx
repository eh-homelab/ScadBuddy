import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NOTIFICATIONS_DISMISSED_KEY, NotificationsOffer } from './NotificationsOffer'

const enableWebPush = vi.hoisted(() => vi.fn(async () => true))
vi.mock('../../agent/webPush', () => ({ enableWebPush }))

const requestPermission = vi.fn(async (): Promise<NotificationPermission> => 'granted')
let permission: NotificationPermission = 'default'

beforeEach(() => {
  permission = 'default'
  requestPermission.mockClear()
  enableWebPush.mockClear()
  vi.stubGlobal('Notification', {
    get permission() {
      return permission
    },
    requestPermission,
  })
  window.localStorage.removeItem(NOTIFICATIONS_DISMISSED_KEY)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('NotificationsOffer', () => {
  it('asks for the permission only when "Turn on" is clicked, then goes', async () => {
    render(<NotificationsOffer />)
    expect(requestPermission).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(requestPermission).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Notifications' })).toBeNull())
  })

  it('subscribes to Web Push once granted, so a closed tab is told too (#815), and not when refused', async () => {
    const { unmount } = render(<NotificationsOffer />)
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(enableWebPush).toHaveBeenCalledTimes(1))
    unmount()
    requestPermission.mockResolvedValueOnce('denied')
    render(<NotificationsOffer />)
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Notifications' })).toBeNull())
    expect(enableWebPush).toHaveBeenCalledTimes(1)
  })

  it('remembers "Not now" without asking', () => {
    const { unmount } = render(<NotificationsOffer />)
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(screen.queryByRole('region', { name: 'Notifications' })).toBeNull()
    expect(requestPermission).not.toHaveBeenCalled()
    unmount()
    render(<NotificationsOffer />)
    expect(screen.queryByRole('region', { name: 'Notifications' })).toBeNull()
  })

  it('is not offered once the browser has an answer, or without the API', () => {
    permission = 'denied'
    const { unmount } = render(<NotificationsOffer />)
    expect(screen.queryByRole('region', { name: 'Notifications' })).toBeNull()
    unmount()
    vi.unstubAllGlobals()
    render(<NotificationsOffer />)
    expect(screen.queryByRole('region', { name: 'Notifications' })).toBeNull()
  })
})
