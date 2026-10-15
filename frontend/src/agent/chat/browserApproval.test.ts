import { describe, expect, it } from 'vitest'
import { browserNavigation } from './browserApproval'

describe('browserNavigation (#982)', () => {
  it("reads a remote plugin's browser_navigate URL from the approval summary, classic or durable", () => {
    expect(browserNavigation('mcp__playwright__browser_navigate {"url":"https://cults3d.com/x?a=1"}')).toEqual({
      url: 'https://cults3d.com/x?a=1',
      origin: 'https://cults3d.com',
    })
    expect(browserNavigation('mcp__durable__playwright__browser_navigate {"url":"http://printer.lan:8080/"}')).toEqual({
      url: 'http://printer.lan:8080/',
      origin: 'http://printer.lan:8080',
    })
  })

  it('is not one for the bridge, another tool, a summary without a whole URL, or a non-web URL', () => {
    expect(browserNavigation('mcp__scadbuddy__browser_navigate {"url":"https://a.example/"}')).toBeUndefined()
    expect(browserNavigation('mcp__playwright__browser_click {"url":"https://a.example/"}')).toBeUndefined()
    expect(browserNavigation('mcp__playwright__browser_navigate {"url":"https://a.exa…')).toBeUndefined()
    expect(browserNavigation('mcp__playwright__browser_navigate {"url":"file:///etc/passwd"}')).toBeUndefined()
    expect(browserNavigation('Send name-keychain to Bambuddy, "url":"https://a.example/"')).toBeUndefined()
  })

  it('reads only a top-level url, from an input that is exactly {url} and written as the browser writes it', () => {
    // A nested url must never be shown while the call opens the top-level one (review of #3654).
    const nav = 'mcp__playwright__browser_navigate '
    expect(browserNavigation(`${nav}{"opts":{"url":"https://makerworld.com/"},"url":"https://evil.example/"}`)).toBeUndefined()
    expect(browserNavigation(`${nav}{"url":"https://evil.example/","opts":{"url":"https://makerworld.com/"}}`)).toBeUndefined()
    expect(browserNavigation(`${nav}{"opts":{"url":"https://makerworld.com/"}}`)).toBeUndefined()
    expect(browserNavigation(`${nav}{"url":"https://Cults3D.com./x"}`)).toBeUndefined()
    expect(browserNavigation(`${nav}{"url":"https://a.example/"} trailing`)).toBeUndefined()
  })
})
