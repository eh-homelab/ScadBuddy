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
    void openPlaidLink('link-sandbox-1').catch(() => {})
    expect(linkScripts()).toHaveLength(1)
  })
})
