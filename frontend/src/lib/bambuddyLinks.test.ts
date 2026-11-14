import { describe, expect, it } from 'vitest'
import { bambuddyBase, bambuddyLink, framingOrigin, webUrls } from './bambuddyLinks'

const API = 'http://bambuddy.bambuddy.svc.cluster.local:8000'
const SSO = 'https://bambuddy.sso.example'
const LAN = 'https://bambuddy.lan.example'

describe('webUrls', () => {
  it('lists the web URLs, default first, without trailing slashes', () => {
    expect(webUrls({ bambuddy_url: API, bambuddy_web_urls: ` ${SSO}/ ,, ${LAN}` })).toEqual([SSO, LAN])
  })

  it('falls back to the API URL when none is set', () => {
    expect(webUrls({ bambuddy_url: `${API}/`, bambuddy_web_urls: null })).toEqual([API])
    expect(webUrls(null)).toEqual([])
  })
})

describe('bambuddyBase', () => {
  it('is the framing Bambuddy when it is listed', () => {
    expect(bambuddyBase([SSO, LAN], LAN)).toBe(LAN)
  })

  it('is the default at the top level or in an unlisted frame', () => {
    expect(bambuddyBase([SSO, LAN], null)).toBe(SSO)
    expect(bambuddyBase([SSO, LAN], 'https://evil.example')).toBe(SSO)
    expect(bambuddyBase([], null)).toBeNull()
  })

  // #6212 — ScadBuddy is reached on more than one hostname, each beside one of Bambuddy's.
  it("is the listed Bambuddy beside the page's host", () => {
    expect(bambuddyBase([SSO, LAN], null, 'scadbuddy.lan.example')).toBe(LAN)
    expect(bambuddyBase([SSO, LAN], null, 'scadbuddy.sso.example')).toBe(SSO)
    expect(bambuddyBase([SSO, 'http://192.168.1.5:8000'], null, '192.168.1.5')).toBe('http://192.168.1.5:8000')
  })

  it('is the framing Bambuddy before the one beside the page', () => {
    expect(bambuddyBase([SSO, LAN], SSO, 'scadbuddy.lan.example')).toBe(SSO)
  })

  it('is the default when no listed Bambuddy is beside the page', () => {
    expect(bambuddyBase([SSO, LAN], null, 'scadbuddy.evil.example')).toBe(SSO)
    expect(bambuddyBase([SSO, LAN], null, 'lan.example')).toBe(SSO)
    expect(bambuddyBase([SSO, LAN], null, 'localhost')).toBe(SSO)
    expect(bambuddyBase([SSO, 'http://10.0.0.5'], null, '192.168.0.5')).toBe(SSO)
  })

  // #6227 — a parent of one label is a bare TLD (`example` stands for `com`) or a LAN
  // suffix, shared by unrelated names.
  it('is the default on a two-label host: a bare TLD makes no sibling', () => {
    expect(bambuddyBase([SSO, 'https://bambuddy.example'], null, 'scadbuddy.example')).toBe(SSO)
    expect(bambuddyBase([SSO, 'https://other.example'], null, 'scadbuddy.example')).toBe(SSO)
    expect(bambuddyBase([SSO, 'https://bambuddy.lan'], null, 'scadbuddy.lan')).toBe(SSO)
  })

  it('strips one trailing dot from a fully qualified host before it matches', () => {
    expect(bambuddyBase([SSO, 'https://other.example.'], null, 'scadbuddy.example.')).toBe(SSO)
    expect(bambuddyBase([SSO, LAN], null, 'scadbuddy.lan.example.')).toBe(LAN)
  })

  // The code has no public-suffix list, so a suffix of two labels (`co.uk`-style) still
  // reads as a shared parent. Only listed URLs can be picked, so the cost is a wrong
  // choice among them, never a link to an unlisted host.
  it('still takes a sibling under a two-label public suffix (a known limit)', () => {
    expect(bambuddyBase([SSO, 'https://other.co.example'], null, 'scadbuddy.co.example')).toBe(
      'https://other.co.example',
    )
  })

  it('is the same host on another port on a two-label host', () => {
    expect(bambuddyBase([SSO, 'http://scadbuddy.lan:8000'], null, 'scadbuddy.lan')).toBe('http://scadbuddy.lan:8000')
  })

  it('is the Bambuddy beside the page when the framing origin is not listed', () => {
    expect(bambuddyBase([SSO, LAN], 'https://bambuddy.unlisted.example', 'scadbuddy.lan.example')).toBe(LAN)
  })
})

describe('bambuddyLink', () => {
  it('moves a server link onto the framing Bambuddy', () => {
    expect(bambuddyLink(`${SSO}/queue/7`, [SSO, LAN], LAN)).toBe(`${LAN}/queue/7`)
  })

  it('leaves it alone outside a listed frame', () => {
    expect(bambuddyLink(`${SSO}/queue`, [SSO, LAN], null)).toBe(`${SSO}/queue`)
    expect(bambuddyLink(`${SSO}/queue`, [SSO, LAN], 'https://evil.example')).toBe(`${SSO}/queue`)
  })

  it("moves a server link onto the Bambuddy beside the page's host (#6212)", () => {
    expect(bambuddyLink(`${SSO}/library`, [SSO, LAN], null, 'scadbuddy.lan.example')).toBe(`${LAN}/library`)
    expect(bambuddyLink(`${SSO}/library`, [SSO, LAN], null, 'scadbuddy.sso.example')).toBe(`${SSO}/library`)
  })

  it('leaves a link on another host alone', () => {
    expect(bambuddyLink(`${SSO}.evil/queue`, [SSO, LAN], LAN)).toBe(`${SSO}.evil/queue`)
    expect(bambuddyLink('https://other.example/queue', [SSO, LAN], LAN)).toBe('https://other.example/queue')
  })
})

describe('framingOrigin', () => {
  it('is null at the top level', () => {
    expect(framingOrigin(false)).toBeNull()
  })

  it('is the referrer origin when the browser has no ancestorOrigins', () => {
    Object.defineProperty(document, 'referrer', { value: `${LAN}/external/3`, configurable: true })
    expect(framingOrigin(true)).toBe(LAN)
    Object.defineProperty(document, 'referrer', { value: '', configurable: true })
  })
})
