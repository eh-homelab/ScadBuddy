import { describe, expect, it } from 'vitest'
import { browserNavigation } from './browserApproval'

describe('browserNavigation (#982)', () => {
  it("reads a remote plugin's browser_navigate URL from the approval summary, classic or durable", () => {
    expect(browserNavigation('mcp__playwright__browser_navigate {"url":"https://Cults3D.com./x?a=1"}')).toEqual({
      url: 'https://cults3d.com./x?a=1',
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
})
