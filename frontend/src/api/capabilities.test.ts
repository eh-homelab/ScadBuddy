import { describe, expect, it } from 'vitest'
import { seedStoredPackage } from '../mocks/aiPlugins'
import { server } from '../mocks/server'
import { aiPlugins } from './aiPlugins'
import { aiCapabilities } from './capabilities'
import { ApiError } from './client'

type Sent = { method: string; path: string; body: string }

/** Every request the client sends while `run` runs, as it left the client. */
async function sent(run: () => Promise<unknown>): Promise<Sent[]> {
  const requests: Promise<Sent>[] = []
  const listener = ({ request }: { request: Request }) => {
    const copy = request.clone()
    requests.push(copy.text().then((body) => ({ method: copy.method, path: new URL(copy.url).pathname, body })))
  }
  server.events.on('request:start', listener)
  try {
    await run()
  } finally {
    // Only this test's: another listener on the shared server stays (#3457).
    server.events.removeListener('request:start', listener)
  }
  return Promise.all(requests)
}

const BASE = '/api/v1/ai/capabilities'

describe('aiCapabilities', () => {
  it('sends each write as the agent expects', async () => {
    await aiCapabilities.refresh('remote:hindsight')

    expect(await sent(() => aiCapabilities.setItem('remote:hindsight', 'files.list', 'off'))).toEqual([
      { method: 'PUT', path: `${BASE}/remote%3Ahindsight/items/files.list`, body: '{"access":"off"}' },
    ])
    expect(await sent(() => aiCapabilities.resetItem('remote:hindsight', 'files.list'))).toEqual([
      { method: 'DELETE', path: `${BASE}/remote%3Ahindsight/items/files.list`, body: '' },
    ])
    expect(await sent(() => aiCapabilities.setEnabled('builtin:http', false))).toEqual([
      { method: 'PUT', path: `${BASE}/builtin%3Ahttp`, body: '{"enabled":false}' },
    ])
    expect(await sent(() => aiCapabilities.refresh('remote:hindsight'))).toEqual([
      { method: 'POST', path: `${BASE}/remote%3Ahindsight/refresh`, body: '' },
    ])
    expect(await sent(() => aiCapabilities.details('remote:hindsight', 'a/b').catch(() => undefined))).toEqual([
      expect.objectContaining({ method: 'GET', path: `${BASE}/remote%3Ahindsight/items/a%2Fb` }),
    ])
  })

  it("a package server's id is one encoded segment", async () => {
    expect(await sent(() => aiCapabilities.setItem('package:greeter/mcp:docs', 'search', 'read').catch(() => undefined))).toEqual([
      { method: 'PUT', path: `${BASE}/package%3Agreeter%2Fmcp%3Adocs/items/search`, body: '{"access":"read"}' },
    ])
  })

  it("the mock's package sources read the package's state, as GET /plugin-packages has it", async () => {
    seedStoredPackage('greeter')
    const pkg = (await aiCapabilities.list()).find((s) => s.id === 'package:greeter')!
    expect(pkg).toMatchObject({ kind: 'package', enabled: true, switchable: true, problem: null })
    expect(pkg.items.map((i) => [i.id, i.access])).toEqual([
      ['hook:Stop:0f1e2d3c4b5a6978', 'on'],
      ['skill:hello', 'on'],
      ['skill:wave', 'on'],
      ['subagent:helper', 'on'],
    ])
    expect(pkg.children.map((c) => c.id)).toEqual(['package:greeter/mcp:mem'])

    const off = await aiCapabilities.setEnabled('package:greeter', false)
    expect((await aiPlugins.listPackages()).find((p) => p.name === 'greeter')?.enabled).toBe(false)
    expect(off.children[0]!.enabled).toBe(true)
    expect(off.items[1]).toMatchObject({ access: 'off', own_access: 'on', reason: 'Off while "greeter" is switched off' })
    const refused = await aiCapabilities.refresh('package:greeter/mcp:mem').catch((e: unknown) => e)
    expect((refused as ApiError).status).toBe(409)

    await aiCapabilities.setEnabled('package:greeter', true)
    const { source } = await aiCapabilities.refresh('package:greeter/mcp:mem')
    expect(source.items.find((i) => i.id === 'recall')?.harness_name).toBe('mcp__plugin_greeter_mem__recall')
    const after = await aiCapabilities.setItem('package:greeter/mcp:mem', 'recall', 'read')
    expect(after.items.find((i) => i.id === 'recall')).toMatchObject({ access: 'read', overridden: true })
  })

  it('answers an ApiError with the agent\'s detail on 409', async () => {
    const error = await aiCapabilities.setEnabled('builtin:scadbuddy', false).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(409)
    expect((error as ApiError).detail).toBe('"builtin:scadbuddy" cannot be switched off or on')
  })
})
