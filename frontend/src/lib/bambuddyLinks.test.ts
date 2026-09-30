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
})

describe('bambuddyLink', () => {
  it('moves a server link onto the framing Bambuddy', () => {
    expect(bambuddyLink(`${SSO}/queue/7`, [SSO, LAN], LAN)).toBe(`${LAN}/queue/7`)
  })

  it('leaves it alone outside a listed frame', () => {
    expect(bambuddyLink(`${SSO}/queue`, [SSO, LAN], null)).toBe(`${SSO}/queue`)
    expect(bambuddyLink(`${SSO}/queue`, [SSO, LAN], 'https://evil.example')).toBe(`${SSO}/queue`)
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
