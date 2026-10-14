import { describe, expect, it } from 'vitest'
import { browserGuard, type BrowserOriginsAccess } from '../src/browser/guard.js'
import {
  type ApprovedScope,
  type BrowserLists,
  checkRules,
  compile,
  DEFAULT_LISTS,
  judge,
  listsOf,
  pageUrls,
  RuleError,
} from '../src/browser/origins.js'

// #982: where the headless browser (the Playwright MCP plugin) may go, by its allow and
// deny lists and the origins approved in a session.

const none = new Map<string, ApprovedScope>()
const lists = (allow: string[], deny: string[] = []) => compile({ allow, deny })
const kind = (l: ReturnType<typeof compile>, url: string, approved = none, use: 'navigate' | 'read' = 'navigate') =>
  judge(l, approved, url, use).kind

describe('rules', () => {
  it('accepts origins, hosts, wildcards and address ranges, and names the first bad rule', () => {
    expect(checkRules([' https://makerworld.com ', 'github.com', '*.printables.com', '10.0.0.0/8', '::1', 'http://[::1]:8080'], 'allow')).toEqual([
      'https://makerworld.com',
      'github.com',
      '*.printables.com',
      '10.0.0.0/8',
      '::1',
      'http://[::1]:8080',
    ])
    for (const bad of ['', 'https://a.com/path', 'ftp://a.com', 'a*.com', '*.', 'a.com:8080', '10.0.0.0/40', 'user@a.com', 'https://u:p@a.com']) {
      expect(() => checkRules([bad], 'deny'), bad).toThrow(RuleError)
    }
    expect(() => checkRules(['github.com', 'nope nope'], 'allow')).toThrow(/allow list: "nope nope"/)
  })

  it('drops duplicates, and caps the list', () => {
    expect(checkRules(['github.com', 'github.com '], 'allow')).toEqual(['github.com'])
    expect(() => checkRules(Array.from({ length: 201 }, (_, i) => `h${i}.example`), 'allow')).toThrow(/more than 200/)
  })

  it('reads the defaults when nothing (or something unreadable) is stored', () => {
    expect(listsOf(undefined)).toEqual(DEFAULT_LISTS)
    expect(listsOf({ allow: 'x', deny: [] })).toEqual({ allow: DEFAULT_LISTS.allow, deny: [] })
    expect(listsOf({ allow: ['a.com'], deny: ['b.com'] })).toEqual({ allow: ['a.com'], deny: ['b.com'] })
  })
})

describe('judging a URL', () => {
  it('allows a listed host on https only, on its default port, and asks about the rest', () => {
    const l = lists(['makerworld.com'])
    expect(kind(l, 'https://makerworld.com/en/models/171292#profileId-188094')).toBe('allowed')
    expect(kind(l, 'https://MAKERWORLD.com./x')).toBe('allowed')
    expect(kind(l, 'http://makerworld.com/')).toBe('ask')
    expect(kind(l, 'https://makerworld.com:8443/')).toBe('ask')
    expect(kind(l, 'https://www.makerworld.com/')).toBe('ask')
  })

  it('refuses lookalike hosts: whole labels only', () => {
    const l = lists(['makerworld.com', '*.printables.com', 'https://github.com'])
    for (const url of [
      'https://makerworld.com.evil.example/',
      'https://evilmakerworld.com/',
      'https://printables.com.evil.example/',
      'https://evilprintables.com/',
      'https://github.com.evil.example/',
      'https://github.com@evil.example/',
    ]) {
      expect(kind(l, url), url).not.toBe('allowed')
    }
    expect(kind(l, 'https://www.printables.com/model/1')).toBe('allowed')
    expect(kind(l, 'https://printables.com/')).toBe('ask') // a wildcard is subdomains only
  })

  it('matches an origin rule exactly: scheme, host and port', () => {
    const l = lists(['http://printer.lan:8080'])
    expect(kind(l, 'http://printer.lan:8080/status')).toBe('allowed')
    expect(kind(l, 'https://printer.lan:8080/')).toBe('ask')
    expect(kind(l, 'http://printer.lan/')).toBe('ask')
  })

  it('deny wins over allow, on every scheme and port, and names its rule', () => {
    const l = lists(['*.example.com', 'https://admin.example.com'], ['admin.example.com'])
    expect(judge(l, none, 'https://admin.example.com/', 'navigate')).toEqual({
      kind: 'denied',
      origin: 'https://admin.example.com',
      rule: 'admin.example.com',
    })
    expect(kind(l, 'http://admin.example.com:9000/')).toBe('denied')
    expect(kind(l, 'https://shop.example.com/')).toBe('allowed')
  })

  it('the default deny list refuses loopback, link-local, metadata and cluster names in any spelling', () => {
    const l = compile(DEFAULT_LISTS)
    for (const url of [
      'http://localhost:8080/',
      'http://LOCALHOST./',
      'http://app.localhost/',
      'http://127.0.0.1/',
      'http://2130706433/', // 127.0.0.1 as one number
      'http://0x7f.1/',
      'http://[::1]/',
      'http://[::ffff:127.0.0.1]/',
      'http://169.254.169.254/latest/meta-data/',
      'http://[::ffff:a9fe:a9fe]/',
      'http://[fe80::1]/',
      'http://metadata.google.internal/',
      'http://metadata/',
      'http://scadbuddy.bambuddy.svc.cluster.local:8080/',
      'http://scadbuddy.bambuddy.svc/',
      'http://0.0.0.0:8080/',
    ]) {
      expect(kind(l, url), url).toBe('denied')
    }
    expect(kind(l, 'https://makerworld.com/')).toBe('allowed')
    expect(kind(l, 'https://example.org/')).toBe('ask')
    expect(kind(l, 'http://192.168.1.20/')).toBe('ask') // a LAN host asks; it is not refused
  })

  it('refuses what is not an http(s) page, and a URL carrying credentials', () => {
    const l = lists(['github.com'])
    expect(kind(l, 'file:///etc/passwd')).toBe('refused')
    expect(kind(l, 'javascript:alert(1)')).toBe('refused')
    expect(kind(l, 'not a url')).toBe('refused')
    expect(kind(l, 'https://me:secret@github.com/')).toBe('refused')
  })

  it('an approval for the session opens without asking; one approved once may be read, not opened again', () => {
    const l = lists([])
    const approved = new Map<string, ApprovedScope>([
      ['https://a.example', 'session'],
      ['https://b.example', 'once'],
    ])
    expect(kind(l, 'https://a.example/x', approved)).toBe('allowed')
    expect(kind(l, 'https://b.example/x', approved)).toBe('ask')
    expect(kind(l, 'https://b.example/x', approved, 'read')).toBe('allowed')
    expect(kind(l, 'https://c.example/x', approved, 'read')).toBe('ask')
  })
})

describe('page URLs in a result', () => {
  it("reads Playwright MCP's Page URL lines, raw or JSON-escaped in the untrusted envelope", () => {
    const raw = '### Page\n- Page URL: https://a.example/x?y=1\n- Page Title: A'
    expect(pageUrls(raw)).toEqual(['https://a.example/x?y=1'])
    expect(pageUrls(JSON.stringify({ untrusted_data: { content: raw } }))).toEqual(['https://a.example/x?y=1'])
    expect(pageUrls('no page here')).toEqual([])
  })
})

/** An in-memory store, as browser/store.ts keeps them in Postgres. */
function memoryStore(lists: BrowserLists): BrowserOriginsAccess & { rows: Map<string, Map<string, ApprovedScope>> } {
  const rows = new Map<string, Map<string, ApprovedScope>>()
  return {
    rows,
    lists: () => Promise.resolve(lists),
    approved: (id) => Promise.resolve(new Map(rows.get(id) ?? [])),
    approve: (id, origin, scope) => {
      const session = rows.get(id) ?? new Map<string, ApprovedScope>()
      if (session.get(origin) !== 'session') session.set(origin, scope)
      rows.set(id, session)
      return Promise.resolve(true)
    },
  }
}

describe('the guard a turn applies', () => {
  const SESSION = '00000000-0000-4000-8000-000000000001'
  const nav = 'mcp__playwright__browser_navigate'
  const page = (url: string) => `### Page\n- Page URL: ${url}\n- Page Title: T\n\nsome page text`

  it("covers every remote plugin's browser_* tools, and nothing else", () => {
    const guard = browserGuard({ store: memoryStore(DEFAULT_LISTS), sessionId: SESSION, plugins: ['playwright'] })
    expect(guard.covers(nav)).toBe(true)
    expect(guard.covers('mcp__playwright__browser_snapshot')).toBe(true)
    expect(guard.covers('mcp__playwright__other')).toBe(false)
    expect(guard.covers('mcp__scadbuddy__browser_navigate')).toBe(false) // the user's own tab, the bridge
    expect(guard.covers('mcp__playwright2__browser_navigate')).toBe(false)
  })

  it('judges a navigation: allowed, denied naming the rule, or outward', async () => {
    const guard = browserGuard({ store: memoryStore({ allow: ['makerworld.com'], deny: ['169.254.0.0/16'] }), sessionId: SESSION, plugins: ['playwright'] })
    expect(await guard.check(nav, { url: 'https://makerworld.com/en/models/1' })).toMatchObject({ allow: expect.any(String) as unknown })
    expect(await guard.check(nav, { url: 'http://169.254.169.254/' })).toEqual({
      deny: expect.stringContaining('deny rule "169.254.0.0/16"') as unknown,
    })
    expect(await guard.check(nav, { url: 'https://example.org/' })).toEqual({
      outward: expect.stringContaining('https://example.org') as unknown,
    })
    // A call that names no page is left to its tier.
    expect(await guard.check('mcp__playwright__browser_click', { ref: 'e1' })).toBeUndefined()
  })

  it('withholds a page reached by redirect that is on neither list, and one that is denied', async () => {
    const guard = browserGuard({ store: memoryStore({ allow: ['makerworld.com'], deny: ['evil.example'] }), sessionId: SESSION, plugins: ['playwright'] })
    const input = { url: 'https://makerworld.com/r' }
    expect(await guard.screen(nav, input, page('https://makerworld.com/x'))).toBeUndefined()
    expect(await guard.screen(nav, input, page('https://elsewhere.example/'))).toMatch(
      /withheld.*elsewhere\.example.*not on the allow list.*browser_navigate/s,
    )
    expect(await guard.screen('mcp__playwright__browser_click', {}, page('https://evil.example/'))).toMatch(/withheld.*deny rule "evil.example"/)
    expect(await guard.screen('mcp__playwright__browser_snapshot', {}, page('about:blank'))).toBeUndefined()
  })

  it('a navigation approved once is remembered, so its pages can be read but it asks again', async () => {
    const store = memoryStore({ allow: [], deny: [] })
    const guard = browserGuard({ store, sessionId: SESSION, plugins: ['playwright'] })
    const input = { url: 'https://example.org/a' }
    expect(await guard.check(nav, input)).toMatchObject({ outward: expect.any(String) as unknown })
    // It ran, so the user approved it: its page is shown, and later reads of it too.
    expect(await guard.screen(nav, input, page('https://example.org/a'))).toBeUndefined()
    expect(store.rows.get(SESSION)?.get('https://example.org')).toBe('once')
    expect(await guard.screen('mcp__playwright__browser_snapshot', {}, page('https://example.org/b'))).toBeUndefined()
    expect(await guard.check(nav, { url: 'https://example.org/c' })).toMatchObject({ outward: expect.any(String) as unknown })
    // Approved for the chat: it opens without asking.
    await store.approve(SESSION, 'https://example.org', 'session')
    expect(await guard.check(nav, { url: 'https://example.org/c' })).toMatchObject({ allow: expect.any(String) as unknown })
  })

  it('without a session (a flow run), an approved navigation is readable for that call only', async () => {
    const store = memoryStore({ allow: [], deny: [] })
    const guard = browserGuard({ store, plugins: ['playwright'] })
    expect(await guard.screen(nav, { url: 'https://example.org/a' }, page('https://example.org/a'))).toBeUndefined()
    expect(await guard.screen('mcp__playwright__browser_snapshot', {}, page('https://example.org/a'))).toMatch(/withheld/)
    expect(store.rows.size).toBe(0)
  })
})
