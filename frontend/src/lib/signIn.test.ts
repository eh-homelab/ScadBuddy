import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HttpResponse, http } from 'msw'
import { mockLoginEnabled, mockSignedIn } from '../mocks/features/auth'
import { server } from '../mocks/server'
import { CrossSiteFrameError, SIGNED_IN, SIGN_IN_FAILED, SignInBlockedError, refreshAuthSession, signIn, signOut } from './signIn'

// #4975 — signing in from inside Bambuddy's frame (spec 2026-11-01 §6.2).

function fakePopup() {
  return { closed: false } as unknown as Window
}

function post(source: unknown, data: unknown, origin = window.location.origin) {
  window.dispatchEvent(new MessageEvent('message', { data, origin, source: source as Window }))
}

describe('signIn from a frame', () => {
  beforeEach(() => mockLoginEnabled())
  afterEach(() => vi.restoreAllMocks())

  it('opens the login in a popup inside the click', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(fakePopup())
    void signIn(true)
    expect(open).toHaveBeenCalledWith('/api/v1/auth/login?return_to=%2F&popup=true', 'scadbuddy-sign-in', expect.any(String))
  })

  it('is an error the user sees when the popup is blocked', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    await expect(signIn(true)).rejects.toBeInstanceOf(SignInBlockedError)
  })

  it('resolves on the popup’s own message and reads the session', async () => {
    const popup = fakePopup()
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const pending = signIn(true)
    mockSignedIn()
    post(popup, { type: SIGNED_IN })
    const session = await pending
    expect(session?.signed_in).toBe(true)
  })

  it('says to open a tab when the popup signed in but this cross-site frame still is not (§6.3)', async () => {
    const popup = fakePopup()
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const pending = signIn(true)
    post(popup, { type: SIGNED_IN })
    await expect(pending).rejects.toBeInstanceOf(CrossSiteFrameError)
  })

  it('ignores a message from another window or origin', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      const popup = fakePopup()
      vi.spyOn(window, 'open').mockReturnValue(popup)
      let settled = false
      const pending = signIn(true).finally(() => (settled = true))
      post({}, { type: SIGN_IN_FAILED, detail: 'forged' })
      post(popup, { type: SIGN_IN_FAILED, detail: 'forged' }, 'https://evil.test')
      await Promise.resolve()
      expect(settled).toBe(false)
      post(popup, { type: SIGN_IN_FAILED, detail: 'not allowed' })
      await expect(pending).rejects.toThrow('not allowed')
    } finally {
      vi.useRealTimers()
    }
  })

  it('finds the session by asking when the message never comes', async () => {
    const popup = fakePopup()
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const pending = signIn(true)
    mockSignedIn()
    expect((await pending)?.signed_in).toBe(true)
  })
})

describe('the session store', () => {
  it('reads the session', async () => {
    mockSignedIn()
    expect((await refreshAuthSession())?.user?.name).toBe('Alice')
  })
})

describe('signOut', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(navigator, 'serviceWorker')
  })

  it("forgets this browser's push subscription before the session ends, so the next person's is not refused (#815)", async () => {
    mockSignedIn()
    const order: string[] = []
    const endpoint = 'https://fcm.googleapis.com/fcm/send/abc'
    const subscription = { endpoint, unsubscribe: () => (order.push('unsubscribe'), Promise.resolve(true)) }
    const registration = { pushManager: { getSubscription: () => Promise.resolve(subscription) } }
    Object.defineProperty(navigator, 'serviceWorker', {
      value: { getRegistration: () => Promise.resolve(registration) },
      configurable: true,
    })
    vi.stubGlobal('PushManager', class {})
    server.use(
      http.delete('/api/v1/ai/push/subscription', ({ request }) => {
        order.push(`delete ${new URL(request.url).searchParams.get('endpoint')}`)
        return new HttpResponse(null, { status: 204 })
      }),
      http.post('/api/v1/auth/logout', () => {
        order.push('logout')
        return HttpResponse.json({ end_session_url: null })
      }),
    )
    await signOut()
    expect(order).toEqual([`delete ${endpoint}`, 'unsubscribe', 'logout'])
  })
})
