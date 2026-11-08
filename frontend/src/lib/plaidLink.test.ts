import { afterEach, describe, expect, it } from 'vitest'
import { openPlaidLink, PLAID_LINK_SCRIPT } from './plaidLink'

function linkScripts(): HTMLScriptElement[] {
  return [...document.head.querySelectorAll('script')].filter((s) => s.src === PLAID_LINK_SCRIPT)
}

describe('openPlaidLink (#4961)', () => {
  afterEach(() => {
    for (const s of linkScripts()) s.remove()
    delete window.Plaid
  })

  it("loads Plaid's own script from cdn.plaid.com, and a failed load can be tried again", async () => {
    const opening = openPlaidLink('link-sandbox-1')
    const [script] = linkScripts()
    expect(script).toBeDefined()
    script!.dispatchEvent(new Event('error'))
    await expect(opening).rejects.toThrow('Plaid Link did not load')
    expect(linkScripts()).toHaveLength(0)
    const again = openPlaidLink('link-sandbox-1')
    expect(linkScripts()).toHaveLength(1)
    // Fail it too, so the next test starts with no load in flight.
    linkScripts()[0]!.dispatchEvent(new Event('error'))
    await expect(again).rejects.toThrow('Plaid Link did not load')
  })

  it('a script that loads without window.Plaid can be tried again', async () => {
    const opening = openPlaidLink('link-sandbox-1')
    const [script] = linkScripts()
    script!.dispatchEvent(new Event('load'))
    await expect(opening).rejects.toThrow('Plaid Link did not load')
    expect(linkScripts()).toHaveLength(0)
    const again = openPlaidLink('link-sandbox-1')
    expect(linkScripts()).toHaveLength(1)
    // Fail it too, so the next test starts with no load in flight.
    linkScripts()[0]!.dispatchEvent(new Event('error'))
    await expect(again).rejects.toThrow('Plaid Link did not load')
  })
})
