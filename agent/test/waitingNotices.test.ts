import { describe, expect, it } from 'vitest'
import { REQUEST_TTL_MS } from '../src/bridge/pairings.js'
import { type AttentionNotice, attentionMessage, NotificationDispatcher, noticeTitle, WAITING_POLL_LEASE, WAITING_POLL_LEASE_S, WAITING_PUSH_WINDOW_MS } from '../src/notify/dispatcher.js'
import { type PushRepo, type PushSubscriptionRow, SENT_KEEP_DAYS } from '../src/notify/pushStore.js'
import { crudGrantRequests, crudPollLease, pairingRequests, waitingNotices } from '../src/notify/waiting.js'
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

const dispatcher = (
  store: PushRepo,
  own: AttentionNotice[],
  waiting: () => Promise<AttentionNotice[]>,
  log: (m: string) => void = () => {},
  ownReads: Owner[] = [],
  pollLease?: (name: string, seconds: number) => Promise<boolean>,
) =>
  new NotificationDispatcher({
    store,
    ...(pollLease ? { pollLease } : {}),
    notices: (o) => {
      ownReads.push(o)
      return Promise.resolve(own)
    },
    waiting,
    subject: 'https://scadbuddy.example',
    hosts: () => Promise.resolve([]),
    log,
  })

describe('waiting requests by Web Push (#6399)', () => {
  it("pushes a grant and a pairing request to a person's subscriptions, never to an agent's", async () => {
    const { store, claims } = recordingStore([sub('b1', owner('browser', 'browser')), sub('o1', owner('oidc', 'ann')), sub('t1', owner('bearer', 'token:p1'))])
    const grant: AttentionNotice = { id: 'run-grant:g1', sessionId: null, reason: 'grant', since: Date.now() - 60_000 }
    const pairing: AttentionNotice = { id: 'pairing:p1', sessionId: null, reason: 'pairing', since: Date.now() }
    await dispatcher(store, [], () => Promise.resolve([grant, pairing])).pass()
    expect(claims.sort()).toEqual(['pairing:p1 -> push:b1', 'pairing:p1 -> push:o1', 'run-grant:g1 -> push:b1', 'run-grant:g1 -> push:o1'])
  })

  it('never pushes a grant again once its claim is swept: a request past the push window is not pushed', async () => {
    // A claim is kept SENT_KEEP_DAYS (2): a grant still waiting 3 days on must not be claimed again.
    const { store, claims } = recordingStore([sub('b1', owner('browser', 'browser'))])
    const day = 24 * 60 * 60_000
    const old: AttentionNotice = { id: 'trigger-grant:g3', sessionId: null, reason: 'grant', since: Date.now() - 3 * day }
    const fresh: AttentionNotice = { id: 'run-grant:g4', sessionId: null, reason: 'grant', since: Date.now() - 1000 }
    const unknown: AttentionNotice = { id: 'run-grant:g5', sessionId: null, reason: 'grant' }
    await dispatcher(store, [], () => Promise.resolve([old, fresh, unknown])).pass()
    expect(claims).toEqual(['run-grant:g4 -> push:b1'])
    expect(WAITING_PUSH_WINDOW_MS).toBeLessThan(SENT_KEEP_DAYS * day)
  })

  it("reads only the grants and pairing requests on the timer's wake, not every owner's pending input", async () => {
    const { store, claims } = recordingStore([sub('b1', owner('browser', 'browser'))])
    const reads: Owner[] = []
    const own: AttentionNotice = { id: 'approval:a1', sessionId: 's', reason: 'approval' }
    const grant: AttentionNotice = { id: 'run-grant:g6', sessionId: null, reason: 'grant', since: Date.now() }
    await dispatcher(store, [own], () => Promise.resolve([grant]), () => {}, reads).pass('waiting')
    expect(reads).toEqual([])
    expect(claims).toEqual(['run-grant:g6 -> push:b1'])
  })

  it("opens no subscription on the timer's wake when nothing young enough waits", async () => {
    const { store } = recordingStore([sub('b1', owner('browser', 'browser'))])
    let opened = 0
    const counting: PushRepo = {
      ...store,
      subscriptions: () => {
        opened += 1
        return store.subscriptions()
      },
    }
    const old: AttentionNotice = { id: 'run-grant:g7', sessionId: null, reason: 'grant', since: Date.now() - 2 * 24 * 60 * 60_000 }
    await dispatcher(counting, [], () => Promise.resolve([old])).pass('waiting')
    expect(opened).toBe(0)
  })

  it("reads the waiting requests on one replica a timer window: another's lease skips it, and claims nothing", async () => {
    const { store, claims } = recordingStore([sub('b1', owner('browser', 'browser'))])
    const leases: string[] = []
    let reads = 0
    const read = () => {
      reads += 1
      return Promise.resolve([])
    }
    const take = (held: boolean | Error) => (name: string, seconds: number) => {
      leases.push(`${name}/${seconds}`)
      return held instanceof Error ? Promise.reject(held) : Promise.resolve(held)
    }
    await dispatcher(store, [], read, () => {}, [], take(false)).pass('waiting')
    await dispatcher(store, [], read, () => {}, [], take(new Error('HTTP 503'))).pass('waiting')
    expect(reads).toBe(0)
    await dispatcher(store, [], read, () => {}, [], take(true)).pass('waiting')
    expect(reads).toBe(1)
    // One lease row per poll name, not a claim per window.
    expect(leases).toEqual(Array(3).fill(`${WAITING_POLL_LEASE}/${WAITING_POLL_LEASE_S}`))
    expect(claims).toEqual([])
  })

  it('takes the poll lease through the CRUD route, and polls on every replica before the API serves it', async () => {
    const seen: { url: string; body: unknown }[] = []
    const fetch = (input: string | URL | Request, init?: RequestInit) => {
      seen.push({ url: String(input), body: JSON.parse(String(init?.body)) })
      return Promise.resolve(new Response(JSON.stringify({ held: false }), { status: 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }))
    }
    const target = { baseUrl: 'http://api.test', fetch: fetch as typeof globalThis.fetch }
    expect(await crudPollLease(target)('waiting-poll', 25)).toBe(false)
    expect(seen).toEqual([{ url: 'http://api.test/api/v1/internal/crud/agent/push/poll-lease', body: { name: 'waiting-poll', seconds: 25 } }])
    expect(await crudPollLease(target, () => false)('waiting-poll', 25)).toBe(true)
    expect(seen).toHaveLength(1)
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
      const body = JSON.stringify({ requests: [{ id: 'trigger-grant:g2', reason: 'grant', since: '2026-11-15T10:00:00Z' }] })
      return Promise.resolve(new Response(body, { status: 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }))
    }
    const expiresAt = new Date('2026-11-15T10:05:00Z')
    const read = waitingNotices([
      crudGrantRequests({ baseUrl: 'http://api.test', fetch: fetch as typeof globalThis.fetch }),
      pairingRequests({ pending: () => Promise.resolve([{ id: 'p9', label: 'Claude Desktop', expiresAt }]) }),
    ])
    expect(await read()).toEqual([
      { id: 'trigger-grant:g2', sessionId: null, reason: 'grant', since: Date.parse('2026-11-15T10:00:00Z') },
      { id: 'pairing:p9', sessionId: null, reason: 'pairing', since: expiresAt.getTime() - REQUEST_TTL_MS },
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
