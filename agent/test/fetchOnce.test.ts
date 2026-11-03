import { createHmac, hkdfSync } from 'node:crypto'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { decide } from '../src/harness/permissions.js'
import { FETCH_GRANT_HEADER, FETCH_GRANT_KEY_INFO, fetchGrantMinter } from '../src/tools/fetchGrant.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { pinnedOutward } from '../src/tools/pinnedOutward.js'
import { effectiveTool, runTool, type Tool, type ToolContext, type ToolOverrides } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// The one-off approval of a host off the asset allowlist (#959): `allow_host` makes a
// fetch_asset call outward whatever lowers the tool's tier, and only an approved call
// mints the grant the backend spends.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const fetchAsset: Tool = ALL_TOOLS.find((t) => t.name === 'fetch_asset')!
const URL_OFF = 'https://icons.example.com/a.svg'
const ONCE = { slug: 'box', url: URL_OFF, allow_host: 'icons.example.com' }

function ctx(overrides: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

/** Settings lowering fetch_asset to `write`. */
const lowered: ToolOverrides = { of: async () => ({ tier: 'write', disabled: false }), disabled: async () => new Set() }

describe('fetch_asset allow_host (#959)', () => {
  it('names the host the backend will hold the fetch to on its approval', async () => {
    const pending = await runTool(fetchAsset, ONCE, ctx())
    expect(firstText(pending)).toMatchObject({
      status: 'pending_approval',
      summary:
        'Fetch https://icons.example.com/a.svg into model "box" as a file asset. icons.example.com is NOT on your ' +
        'asset allowlist: approving lets this one fetch from that host through, and the list stays as it is',
    })
  })

  it('stays gated when Settings lowers the tool below outward', async () => {
    expect(await effectiveTool(fetchAsset, { toolOverrides: lowered }, ONCE)).toMatchObject({ risk: 'outward', gated: true })
    // Without the one-off, the override applies as it does to any tool.
    expect(await effectiveTool(fetchAsset, { toolOverrides: lowered }, { slug: 'box', url: URL_OFF })).toMatchObject({ risk: 'write', gated: false })
    const sent: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/models/box/assets/fetch`, async ({ request }) => {
        sent.push(await request.json())
        return HttpResponse.json({}, { status: 500 })
      }),
    )
    const answer = await runTool(fetchAsset, ONCE, ctx({ toolOverrides: lowered, fetchGrant: () => 'grant' }))
    expect(firstText(answer)).toMatchObject({ status: 'pending_approval' })
    expect(sent).toEqual([])
  })

  it("parks at the harness seam whatever the resolver's tier or a guard's allow", () => {
    const name = 'mcp__scadbuddy__fetch_asset'
    const write = () => 'write' as const
    expect(decide(name, write, ONCE, { allow: 'listed' })).toMatchObject({ decision: 'needs_approval', tier: 'outward' })
    expect(decide(name, write, ONCE)).toMatchObject({ decision: 'needs_approval', tier: 'outward' })
    expect(decide(name, write, { ...ONCE, allow_host: 42 })).toMatchObject({ decision: 'needs_approval' })
    expect(decide(name, write, { slug: 'box', url: URL_OFF })).toMatchObject({ decision: 'allow' })
    expect(pinnedOutward('mcp__scadbuddy__get_asset', ONCE)).toBe(false)
  })

  it('once approved, sends the host and a grant the approval minted', async () => {
    const seen: { body: unknown; grant: string | null }[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/models/box/assets/fetch`, async ({ request }) => {
        seen.push({ body: await request.json(), grant: request.headers.get(FETCH_GRANT_HEADER) })
        return HttpResponse.json({ id: 'a'.repeat(64), name: 'a.svg', kind: 'svg', size: 1, source_url: URL_OFF }, { status: 201 })
      }),
    )
    const minted: [string, string][] = []
    const approved = ctx({ gate: 'harness', fetchGrant: (host, url) => (minted.push([host, url]), 'the-grant') })
    expect((await runTool(fetchAsset, ONCE, approved)).isError).toBeFalsy()
    expect(minted).toEqual([['icons.example.com', URL_OFF]])
    expect(seen).toEqual([{ body: { url: URL_OFF, allow_host: 'icons.example.com' }, grant: 'the-grant' }])
  })

  it('refuses a URL two parsers could read different hosts from, before anything is sent', async () => {
    const approved = ctx({ gate: 'harness', fetchGrant: () => 'grant' })
    for (const url of ['https://allowed.com\\@evil.com/a.svg', 'https://user@allowed.com/a.svg', 'https://allowed.com/a\u0001.svg']) {
      const answer = await runTool(fetchAsset, { slug: 'box', url, allow_host: 'allowed.com' }, approved)
      expect(answer.isError, url).toBe(true)
    }
    // Parsed alike, but the host is evil.com: not the one approved.
    const fragment = await runTool(fetchAsset, { slug: 'box', url: 'https://evil.com#@allowed.com', allow_host: 'allowed.com' }, approved)
    expect(fragment.isError).toBe(true)
    expect(JSON.stringify(fragment.content)).toContain("the URL's host is evil.com")
  })

  it('refuses the one-off without a key to mint its grant', async () => {
    const answer = await runTool(fetchAsset, ONCE, ctx({ gate: 'harness' }))
    expect(answer.isError).toBe(true)
    expect(JSON.stringify(answer.content)).toContain('SCADBUDDY_SECRET_KEY_FILE')
  })
})

describe('the grant format (#959)', () => {
  it('matches the vector backend/tests/test_fetch_grants.py verifies', () => {
    const key = Buffer.from(hkdfSync('sha256', Buffer.alloc(32, 7), Buffer.alloc(0), FETCH_GRANT_KEY_INFO, 32))
    const mint = fetchGrantMinter(key, () => (2_000_000_000 - 600) * 1000, () => 'ab'.repeat(16))
    const grant = mint('icons.example.com', URL_OFF)
    expect(grant).toBe(
      'YXNzZXQtZmV0Y2gtb25jZQppY29ucy5leGFtcGxlLmNvbQplNjgxNTEzNjE2YjkwNTM3YjdmMTEyNzJmZjFkMTE2MDE0Zjg4NDFjZDgxMGZlMmM0NjE5OWRjODcxNDQzMDhiCjIwMDAwMDAwMDAKYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWI' +
        '.bVgHQHSbT1v8DfE8-6pNyOYiUMsvYOfIoaJFf-RSPv0',
    )
    const [body, mac] = grant.split('.')
    expect(createHmac('sha256', key).update(Buffer.from(body!, 'base64url')).digest('base64url')).toBe(mac)
  })
})
