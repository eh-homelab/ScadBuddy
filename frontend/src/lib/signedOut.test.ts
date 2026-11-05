import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchAudit } from '../agent/audit'
import { api } from '../api/client'
import { mockSignedIn } from '../mocks/features/auth'
import { server } from '../mocks/server'
import { refreshAuthSession, returnTo } from './signIn'
import {
  clearSignedOut,
  isSignedOut,
  noteSignedOut,
  resetSignedOut,
  setEnforced,
  SIGNED_OUT_PROBLEM,
  whenSignedIn,
} from './signedOut'

// #4975 phase 8 — the page learning it is signed out under SCADBUDDY_AUTH_MODE=enforce.

const signedOut = () =>
  HttpResponse.json(
    { type: SIGNED_OUT_PROBLEM, title: 'Unauthorized', status: 401, detail: 'Sign in' },
    { status: 401 },
  )

function enforceSession(signedIn: boolean) {
  server.use(
    http.get('/api/v1/auth/session', () =>
      HttpResponse.json({
        signed_in: signedIn,
        user: signedIn ? { principal: 'user:x#a', name: 'A', email: null } : null,
        mode: 'enforce',
        login_enabled: true,
        expires_at: null,
        kind: signedIn ? 'login' : null,
      }),
    ),
  )
}

describe('signed out', () => {
  afterEach(() => resetSignedOut())

  it('counts only under enforce', () => {
    noteSignedOut()
    expect(isSignedOut()).toBe(false)
    setEnforced(true)
    noteSignedOut()
    expect(isSignedOut()).toBe(true)
  })

  it('runs what waited once signed in again', () => {
    setEnforced(true)
    noteSignedOut()
    const go = vi.fn()
    whenSignedIn(go)
    expect(go).not.toHaveBeenCalled()
    clearSignedOut()
    expect(go).toHaveBeenCalledTimes(1)
  })

  it('is what the session read says under enforce, and ends with the sign-in', async () => {
    enforceSession(false)
    await refreshAuthSession()
    expect(isSignedOut()).toBe(true)
    enforceSession(true)
    await refreshAuthSession()
    expect(isSignedOut()).toBe(false)
  })

  it("is the gate's 401 under enforce, and no other 401", async () => {
    enforceSession(true)
    await refreshAuthSession()
    server.use(
      http.get('/api/v1/models', () =>
        HttpResponse.json({ title: 'Unauthorized', status: 401, detail: 'Bambuddy refused the key' }, { status: 401 }),
      ),
    )
    await expect(api.listModels()).rejects.toThrow()
    expect(isSignedOut()).toBe(false)
    server.use(http.get('/api/v1/models', signedOut))
    await expect(api.listModels()).rejects.toThrow()
    await vi.waitFor(() => expect(isSignedOut()).toBe(true))
  })

  it("is the agent's 401 too, on a call outside the API client", async () => {
    enforceSession(true)
    await refreshAuthSession()
    server.use(http.get('/api/v1/ai/audit', signedOut))
    await expect(fetchAudit()).rejects.toThrow()
    await vi.waitFor(() => expect(isSignedOut()).toBe(true))
  })

  it('is not a signed-out answer outside enforce', async () => {
    mockSignedIn()
    await refreshAuthSession()
    server.use(http.get('/api/v1/models', signedOut))
    await expect(api.listModels()).rejects.toThrow()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(isSignedOut()).toBe(false)
  })
})

describe('returnTo (spec 2026-11-01 §5.1)', () => {
  it('keeps a same-origin path', () => {
    expect(returnTo('/m/box?x=1#top')).toBe('/m/box?x=1#top')
  })

  it.each(['//evil.example', '/\\evil.example', '/%2F%2Fevil.example', '/%252F%252Fevil', 'https://evil.example', 'm/box'])(
    'makes %s the root',
    (path) => {
      expect(returnTo(path)).toBe('/')
    },
  )
})
