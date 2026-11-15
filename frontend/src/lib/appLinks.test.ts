import { describe, expect, it } from 'vitest'
import { allowedOrigins } from '../test/appLinks'
import { appOrigins, appUrl } from './appLinks'

const INTERNAL = 'https://scadbuddy.internal.nullreference.io'
const SSO = 'https://scadbuddy.sso.nullreference.io'

describe('appOrigins', () => {
  it("is the public URL's origin and each allowed origin, once each", () => {
    expect(
      appOrigins({ public_url: `${INTERNAL}/`, bootstrap: [allowedOrigins(` ${INTERNAL}, ${SSO} ,,not a url, ftp://x.example`)] }),
    ).toEqual([INTERNAL, SSO])
  })

  it('is empty with nothing set', () => {
    expect(appOrigins(null)).toEqual([])
    expect(appOrigins({ public_url: null, bootstrap: [allowedOrigins(null)] })).toEqual([])
  })
})

describe('appUrl', () => {
  it.each([INTERNAL, SSO])('puts a path on the current origin (%s)', (here) => {
    expect(appUrl('/m/name-keychain?preset=p1', [], here)).toBe(`${here}/m/name-keychain?preset=p1`)
  })

  it.each([INTERNAL, SSO])("moves a link on one of ScadBuddy's origins to %s, keeping its path, query and hash", (here) => {
    expect(appUrl(`${INTERNAL}/edit/o1?x=1#y`, [INTERNAL, SSO], here)).toBe(`${here}/edit/o1?x=1#y`)
    expect(appUrl(`${SSO}/m/a`, [INTERNAL, SSO], here)).toBe(`${here}/m/a`)
  })

  it('returns a link already on the current origin as it is, not rebuilt', () => {
    // A rebuilt link would encode the space as %20.
    expect(appUrl(`${SSO}/m/a b`, [INTERNAL, SSO], SSO)).toBe(`${SSO}/m/a b`)
  })

  it('leaves any other URL as it is', () => {
    expect(appUrl('https://bambuddy.sso.example/library', [INTERNAL], SSO)).toBe('https://bambuddy.sso.example/library')
    expect(appUrl(`${INTERNAL}:8443/m/a`, [INTERNAL], SSO)).toBe(`${INTERNAL}:8443/m/a`)
    expect(appUrl('//evil.example/a', [INTERNAL], SSO)).toBe('//evil.example/a')
    expect(appUrl('not a url', [INTERNAL], SSO)).toBe('not a url')
  })
})
