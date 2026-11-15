import { describe, expect, it } from 'vitest'
import { type AttentionNotice, attentionMessage, NotificationDispatcher, noticeTitle } from '../src/notify/dispatcher.js'
import type { PushRepo, PushSubscriptionRow } from '../src/notify/pushStore.js'
import { crudGrantRequests, pairingRequests, waitingNotices } from '../src/notify/waiting.js'
import type { Owner } from '../src/sessions/protocol.js'

// #6399: a flow grant or an agent's pairing request reaches a person by Web Push with every tab
// closed, as the agent's own requests do; never an agent's subscription.

const owner = (kind: Owner['kind'], id: string): Owner => ({ kind, id, label: id })
const sub = (id: string, who: Owner): PushSubscriptionRow => ({ id, owner: who, endpoint: `https://fcm.googleapis.com/fcm/send/${id}`, keys: { p256dh: Buffer.alloc(65), auth: Buffer.alloc(16) } })

/** A store that records each claim, so a test sees what a pass would send and to whom. */
function recordingStore(subs: PushSubscriptionRow[]) {
  const claims: string[] = []
  const store: PushRepo = {
    vapidKeys: () => Promise.reject(new Error('not reached')),
    subscribe: () => Promise.reject(new Error('not reached')),
    unsubscribe: () => Promise.resolve(true),
    subscriptions: () => Promise.resolve(subs),
    claim: (requestId, target) => {
      claims.push(`${requestId} -> ${target}`)
      // Claimed by "another replica": the send itself is not this test's.
      return Promise.resolve(false)
    },
  }
  return { store, claims }
}

const dispatcher = (store: PushRepo, own: AttentionNotice[], waiting: () => Promise<AttentionNotice[]>, log: (m: string) => void = () => {}) =>
  new NotificationDispatcher({
    store,
    notices: () => Promise.resolve(own),
    waiting,
    subject: 'https://scadbuddy.example',
    hosts: () => Promise.resolve([]),
    log,
  })

describe('waiting requests by Web Push (#6399)', () => {
  it("pushes a grant and a pairing request to a person's subscriptions, never to an agent's", async () => {
    const { store, claims } = recordingStore([sub('b1', owner('browser', 'browser')), sub('o1', owner('oidc', 'ann')), sub('t1', owner('bearer', 'token:p1'))])
    const grant: AttentionNotice = { id: 'run-grant:g1', sessionId: null, reason: 'grant' }
    const pairing: AttentionNotice = { id: 'pairing:p1', sessionId: null, reason: 'pairing' }
    await dispatcher(store, [], () => Promise.resolve([grant, pairing])).pass()
    expect(claims.sort()).toEqual(['pairing:p1 -> push:b1', 'pairing:p1 -> push:o1', 'run-grant:g1 -> push:b1', 'run-grant:g1 -> push:o1'])
  })

  it("still pushes the agent's own requests when the grants cannot be read", async () => {
    const { store, claims } = recordingStore([sub('b1', owner('browser', 'browser'))])
    const logged: string[] = []
    const own: AttentionNotice = { id: 'approval:a1', sessionId: 's', reason: 'approval' }
    await dispatcher(store, [own], () => Promise.reject(new Error('HTTP 503')), (m) => logged.push(m)).pass()
    expect(claims).toEqual(['approval:a1 -> push:b1'])
    expect(logged.join('\n')).toContain('HTTP 503')
  })

  it('reads the grants by id through the CRUD route, and the pairing requests from the store', async () => {
    const seen: string[] = []
    const fetch = (input: string | URL | Request) => {
      seen.push(String(input))
      const body = JSON.stringify({ requests: [{ id: 'trigger-grant:g2', reason: 'grant' }] })
      return Promise.resolve(new Response(body, { status: 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }))
    }
    const read = waitingNotices([
      crudGrantRequests({ baseUrl: 'http://api.test', fetch: fetch as typeof globalThis.fetch }),
      pairingRequests({ pending: () => Promise.resolve([{ id: 'p9', label: 'Claude Desktop', expiresAt: new Date() }]) }),
    ])
    expect(await read()).toEqual([
      { id: 'trigger-grant:g2', sessionId: null, reason: 'grant' },
      { id: 'pairing:p9', sessionId: null, reason: 'pairing' },
    ])
    expect(seen).toEqual(['http://api.test/api/v1/internal/crud/agent/push/waiting'])
  })

  it('reads no grants before the API serves area notify, and leaves out a source that fails', async () => {
    const logged: string[] = []
    const read = waitingNotices(
      [
        crudGrantRequests({ baseUrl: 'http://api.test', fetch: () => Promise.reject(new Error('not reached')) }, () => false),
        () => Promise.reject(new Error('pairings down')),
      ],
      (m) => logged.push(m),
    )
    expect(await read()).toEqual([])
    expect(logged.join('\n')).toContain('pairings down')
  })

  it('titles each kind and opens Needs you, never a session', () => {
    expect(noticeTitle('grant')).toBe('A flow asks for a grant')
    expect(noticeTitle('pairing')).toBe('An agent asks to use ScadBuddy')
    expect(attentionMessage({ id: 'run-grant:g1', sessionId: null, reason: 'grant' }, 'https://sb.example')).toEqual({
      title: 'A flow asks for a grant',
      body: 'Open ScadBuddy to decide it.',
      click: 'https://sb.example/activity#needs-you',
    })
  })
})
