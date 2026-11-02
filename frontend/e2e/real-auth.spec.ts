import { readFileSync } from 'node:fs'
import { expect, test, type APIRequestContext, type BrowserContext, type Frame, type FrameLocator, type Page } from '@playwright/test'

/**
 * #4975 phase 7 (spec 2026-11-01 §11): the login against a real stack, in Chromium, Firefox
 * and WebKit. `e2e-stack/run.sh --auth` brings it up (compose.auth.yaml: the app in
 * `observe` with its login on, the fake issuer, an https edge for `scadbuddy.e2e.test` and
 * `idp.e2e.test`) and runs this in the Playwright image on the stack's network.
 *
 * The Bambuddy replicas are pages Playwright serves itself, with Bambuddy's exact sandbox
 * (as e2e/bambuddyFrame.ts builds one): `bambuddy.e2e.test` is same-site with the app (the
 * site `https://e2e.test`; `test` is not on the Public Suffix List) and
 * `bambuddy.elsewhere.test` is not. Those two are where spec §6.1's and §6.3's assumptions
 * are measured.
 */

const enabled = !!process.env.E2E_AUTH
const state = process.env.E2E_AUTH_STATE ?? ''
const ISSUER = 'https://idp.e2e.test'
const ALICE = `user:${ISSUER}#alice`
const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox'

test.describe('the login on a real stack', () => {
  test.skip(!enabled, 'needs the stack e2e-stack/run.sh --auth brings up')
  // Each test signs in through the issuer at least once, some twice.
  test.describe.configure({ timeout: 90_000 })

  async function session(target: Page | Frame): Promise<{ signed_in: boolean; kind?: string }> {
    return target.evaluate(async () => (await (await fetch('/api/v1/auth/session')).json()) as { signed_in: boolean; kind?: string })
  }

  /** The session as the browser's own cookies make it, without the page (which may be navigating). */
  async function cookieSession(page: Page): Promise<{ signed_in: boolean; kind?: string }> {
    return (await (await page.context().request.get('/api/v1/auth/session')).json()) as { signed_in: boolean; kind?: string }
  }

  async function signInTopLevel(page: Page) {
    await page.goto('/')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('button', { name: 'Account: Alice' })).toBeVisible({ timeout: 30_000 })
  }

  /** A Bambuddy page on `host` framing the app, as Bambuddy's External Link does. */
  async function bambuddy(page: Page, context: BrowserContext, baseURL: string, host: string): Promise<FrameLocator> {
    const top = `https://${host}/external/1`
    await page.route(top, (route) =>
      route.fulfill({
        contentType: 'text/html',
        body: `<!doctype html><iframe src="${baseURL}/" title="ScadBuddy" sandbox="${SANDBOX}"
          style="position: fixed; inset: 0; width: 100%; height: 100%; border: 0"></iframe>`,
      }),
    )
    // Chrome's Local Network Access: the routed page is not on the private network the app
    // is on. Bambuddy on the LAN is; grant it (as e2e/bambuddyFrame.ts does).
    if (context.browser()?.browserType().name() === 'chromium') await context.grantPermissions(['local-network-access'])
    await page.goto(top)
    return page.frameLocator('iframe')
  }

  function appFrame(page: Page): Frame {
    const frame = page.frames().find((candidate) => candidate.url().startsWith('https://scadbuddy.e2e.test'))
    if (!frame) throw new Error('the ScadBuddy frame is not loaded')
    return frame
  }

  test('signs in and out at the top level', async ({ page }) => {
    await signInTopLevel(page)
    const signedIn = await cookieSession(page)
    expect(signedIn).toMatchObject({ signed_in: true, kind: 'login', mode: 'observe' })
    const cookies = await page.context().cookies('https://scadbuddy.e2e.test')
    const cookie = cookies.find((c) => c.name === '__Host-scadbuddy_session')
    expect(cookie).toMatchObject({ secure: true, httpOnly: true, sameSite: 'Strict', path: '/' })

    await page.getByRole('button', { name: 'Account: Alice' }).click()
    await page.getByRole('menuitem', { name: 'Sign out' }).click()
    // rp_logout is on: the issuer's end-session page sends the tab back home.
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible({ timeout: 30_000 })
    expect(await cookieSession(page)).toMatchObject({ signed_in: false })
  })

  test('signs in from a same-site Bambuddy frame, and the frame sends the Strict cookie (§6.1)', async ({ page, context, baseURL }) => {
    const frame = await bambuddy(page, context, baseURL!, 'bambuddy.e2e.test')
    const popup = context.waitForEvent('page')
    await frame.getByRole('button', { name: 'Sign in' }).click()
    // The popup escapes the sandbox, signs in first-party and closes itself.
    await (await popup).waitForEvent('close', { timeout: 30_000 })
    await expect(frame.getByRole('button', { name: 'Account: Alice' })).toBeVisible({ timeout: 15_000 })
    expect(await session(appFrame(page))).toMatchObject({ signed_in: true })
  })

  test('a cross-site Bambuddy frame never gets the cookie, and says to open a tab (§6.3)', async ({ page, context, baseURL }) => {
    const frame = await bambuddy(page, context, baseURL!, 'bambuddy.elsewhere.test')
    const popup = context.waitForEvent('page')
    await frame.getByRole('button', { name: 'Sign in' }).click()
    await (await popup).waitForEvent('close', { timeout: 30_000 })
    await expect(frame.getByText('embedded from another site')).toBeVisible({ timeout: 15_000 })
    expect(await session(appFrame(page))).toMatchObject({ signed_in: false })
    // The popup's own first-party sign-in stands: the same browser is signed in at the top level.
    const tab = await context.newPage()
    await tab.goto('/')
    expect(await cookieSession(tab)).toMatchObject({ signed_in: true })
  })

  test('signing out everywhere ends the other browser’s session', async ({ page, browser }) => {
    await signInTopLevel(page)
    const other = await browser.newContext({ ignoreHTTPSErrors: true })
    try {
      const second = await other.newPage()
      await signInTopLevel(second)
      await second.goto('/settings/system#signed-in-sessions')
      await second.getByRole('button', { name: 'Sign out everywhere' }).click()
      await expect.poll(async () => (await cookieSession(page)).signed_in, { timeout: 15_000 }).toBe(false)
    } finally {
      await other.close()
    }
  })

  test('observe serves a signed-out call and counts it', async ({ playwright, baseURL }) => {
    const anonymous = await playwright.request.newContext({ baseURL, ignoreHTTPSErrors: true })
    try {
      const before = await refused(anonymous)
      const models = await anonymous.get('/api/v1/models')
      expect(models.status()).toBe(200)
      await expect.poll(() => refused(anonymous), { timeout: 10_000 }).toBeGreaterThan(before)
    } finally {
      await anonymous.dispose()
    }
  })

  test('a delegated sign-in link signs a fresh browser in as the session’s person (§7.4)', async ({ page, playwright, baseURL }) => {
    const token = readFileSync(`${state}/agent-token`, 'utf8').trim()
    const agent = await playwright.request.newContext({
      baseURL,
      ignoreHTTPSErrors: true,
      extraHTTPHeaders: { Authorization: `Bearer ${token}` },
    })
    let url: string
    try {
      const minted = await agent.post('/api/v1/auth/delegated-sessions', { data: { principal: ALICE, ttl_seconds: 600 } })
      expect(minted.status(), await minted.text()).toBe(201)
      url = ((await minted.json()) as { url: string }).url
    } finally {
      await agent.dispose()
    }
    expect(url.startsWith('https://scadbuddy.e2e.test/api/v1/auth/delegated?code=')).toBe(true)
    await page.goto(url)
    await page.goto('/')
    expect(await session(page)).toMatchObject({ signed_in: true, kind: 'delegated' })
    // Once only.
    const again = await page.context().request.get(url, { maxRedirects: 0 })
    expect(again.status()).toBe(400)
  })
})

/** The sum of `scadbuddy_auth_would_refuse_total` over every label. */
async function refused(request: APIRequestContext): Promise<number> {
  const text = await (await request.get('/metrics')).text()
  return text
    .split('\n')
    .filter((line) => line.startsWith('scadbuddy_auth_would_refuse_total'))
    .reduce((sum, line) => sum + Number(line.split(' ').at(-1)), 0)
}
