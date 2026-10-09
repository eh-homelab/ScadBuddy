import http from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { UiOriginProbe } from '../src/harness/browserReach.js'

// Which of ScadBuddy's UI origins the headless browser reaches without a login
// (browserReach.ts). Production's SCADBUDDY_PUBLIC_URL sits behind an SSO proxy
// that answers every unauthenticated request with a 302 to its sign-in page,
// while an internal origin answers 200.

type Server = { origin: string; hits: string[]; close: () => Promise<void> }

async function serve(handler: http.RequestListener): Promise<Server> {
  const hits: string[] = []
  const server = http.createServer((req, res) => {
    hits.push(req.url ?? '')
    handler(req, res)
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  return {
    origin: `http://127.0.0.1:${port}`,
    hits,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections()
        server.close(() => resolve())
      }),
  }
}

describe('UiOriginProbe', () => {
  let open: Server
  let sso: Server
  let broken: Server
  let stuck: Server
  let closed: string

  beforeAll(async () => {
    open = await serve((_req, res) => res.writeHead(200, { 'content-type': 'application/json' }).end('{"status":"ok"}'))
    sso = await serve((_req, res) =>
      res.writeHead(302, { location: 'https://authenticate.sso.example/.pomerium/sign_in?x=1' }).end(),
    )
    broken = await serve((_req, res) => res.writeHead(503).end())
    stuck = await serve(() => {
      // never answers
    })
    const gone = await serve(() => undefined)
    closed = gone.origin
    await gone.close()
  })
  afterAll(async () => {
    for (const s of [open, sso, broken, stuck]) await s.close()
  })

  it('reads 2xx as reachable, a redirect as a sign-in, and anything else as unreachable', async () => {
    const probe = new UiOriginProbe({ timeoutMs: 500 })
    const reach = await probe.probe([sso.origin, open.origin, broken.origin, closed, stuck.origin])
    expect(reach).toEqual({
      [sso.origin]: { reach: 'sign-in', detail: '302 to https://authenticate.sso.example' },
      [open.origin]: { reach: 'ok' },
      [broken.origin]: { reach: 'unreachable', detail: 'answered 503' },
      [closed]: { reach: 'unreachable', detail: expect.any(String) },
      [stuck.origin]: { reach: 'unreachable', detail: 'no answer within 500 ms' },
    })
    // It asks /healthz, and follows no redirect.
    expect(open.hits).toEqual(['/healthz'])
    expect(sso.hits).toEqual(['/healthz'])
  })

  it('reads 401 and 403 as a sign-in too', async () => {
    const s = await serve((req, res) => res.writeHead(req.url === '/healthz' ? 401 : 200).end())
    const f = await serve((_req, res) => res.writeHead(403).end())
    try {
      const reach = await new UiOriginProbe().probe([s.origin, f.origin])
      expect(reach[s.origin]).toEqual({ reach: 'sign-in', detail: 'answered 401' })
      expect(reach[f.origin]).toEqual({ reach: 'sign-in', detail: 'answered 403' })
    } finally {
      await s.close()
      await f.close()
    }
  })

  it('keeps an answer for its time to live, then asks again', async () => {
    let now = 1_000
    const probe = new UiOriginProbe({ ttlMs: 30_000, now: () => now })
    const before = open.hits.length
    await probe.probe([open.origin])
    await probe.probe([open.origin])
    expect(open.hits.length).toBe(before + 1)
    now += 30_001
    await probe.probe([open.origin])
    expect(open.hits.length).toBe(before + 2)
  })

  it('shares one request among concurrent turns', async () => {
    const probe = new UiOriginProbe()
    const before = open.hits.length
    await Promise.all([probe.probe([open.origin]), probe.probe([open.origin])])
    expect(open.hits.length).toBe(before + 1)
  })

  it('says on /healthz only whether any origin is reachable, and logs the details when an answer changes', async () => {
    const lines: string[] = []
    const probe = new UiOriginProbe({ log: (line) => lines.push(line) })
    expect(probe.summary()).toBe('not checked')
    await probe.probe([sso.origin])
    expect(probe.summary()).toBe('none reachable')
    expect(lines).toEqual([`headless browser: ${sso.origin} answers sign-in (302 to https://authenticate.sso.example)`])
    await probe.probe([sso.origin, open.origin])
    expect(probe.summary()).toBe('reachable')
    expect(lines.slice(1)).toEqual([`headless browser: ${open.origin} answers ok`])
  })

  it('asks only a bare http(s) origin, at the fixed /healthz path', async () => {
    const probe = new UiOriginProbe()
    const before = open.hits.length
    const reach = await probe.probe([`${open.origin}/evil?x=1`, `file:///etc/passwd`, `${open.origin}/`])
    for (const r of Object.values(reach)) expect(r).toEqual({ reach: 'unreachable', detail: 'not an http(s) origin' })
    expect(open.hits.length).toBe(before)
  })

})
