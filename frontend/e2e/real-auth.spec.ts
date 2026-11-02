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
 *
 * #4975 phase 8: `run.sh --auth` then restarts the app in `enforce` (E2E_AUTH_MODE) and runs
 * the second describe here: an unauthenticated request refused, a signed-out page sent to
 * the sign-in and back, and a revoked session's socket closed with 4401.
 */

const enabled = !!process.env.E2E_AUTH
const mode = process.env.E2E_AUTH_MODE ?? 'observe'
const state = process.env.E2E_AUTH_STATE ?? ''
const ISSUER = 'https://idp.e2e.test'
const ALICE = `user:${ISSUER}#alice`
const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox'

async function session(target: Page | Frame): Promise<{ signed_in: boolean; kind?: string }> {
  return target.evaluate(async () => (await (await fetch('/api/v1/auth/session')).json()) as { signed_in: boolean; kind?: string })
}

/** The session as the browser's own cookies make it, without the page (which may be navigating). */
async function cookieSession(page: Page): Promise<{ signed_in: boolean; kind?: string; mode?: string }> {
  return (await (await page.context().request.get('/api/v1/auth/session')).json()) as {
    signed_in: boolean
    kind?: string
    mode?: string
  }
}

test.describe('the login on a real stack', () => {
  test.skip(!enabled || mode !== 'observe', 'needs the stack e2e-stack/run.sh --auth brings up, in observe')
  // Each test signs in through the issuer at least once, some twice.
  test.describe.configure({ timeout: 90_000 })

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

test.describe('enforce on a real stack (#4975 phase 8)', () => {
  test.skip(!enabled || mode !== 'enforce', 'needs the stack e2e-stack/run.sh --auth brings up, in enforce')
  test.describe.configure({ timeout: 90_000 })

  /** Signed out, the page goes to the sign-in itself; the fake issuer signs Alice in. */
  async function signIn(page: Page, path = '/') {
    await page.goto(path)
    await expect(page.getByRole('button', { name: 'Account: Alice' })).toBeVisible({ timeout: 30_000 })
  }

  test('refuses an unauthenticated request', async ({ playwright, baseURL }) => {
    const anonymous = await playwright.request.newContext({ baseURL, ignoreHTTPSErrors: true })
    try {
      const models = await anonymous.get('/api/v1/models')
      expect(models.status()).toBe(401)
      expect(await models.json()).toMatchObject({ type: 'https://scadbuddy.dev/problems/auth/signed-out' })
      const internal = await anonymous.get('/api/v1/internal/outputs/nope/model.3mf')
      expect(internal.status()).toBe(401)
      expect(internal.headers()['www-authenticate']).toBe('Bearer realm="scadbuddy"')
      // What signs a person in stays open.
      expect((await anonymous.get('/healthz')).status()).toBe(200)
      expect(await (await anonymous.get('/api/v1/auth/session')).json()).toMatchObject({ signed_in: false, mode: 'enforce' })
    } finally {
      await anonymous.dispose()
    }
  })

  test('sends a signed-out page to the sign-in and back to where it was', async ({ page }) => {
    await signIn(page, '/settings/system')
    expect(new URL(page.url()).pathname).toBe('/settings/system')
    expect(await cookieSession(page)).toMatchObject({ signed_in: true, mode: 'enforce' })
  })

  test('a revoked session closes its socket with 4401 and the page asks to sign in again', async ({ page, browser }) => {
    await signIn(page)
    // A socket of the page's own, to read the close code the server sends.
    const url = `wss://${new URL(page.url()).host}/api/v1/ws`
    await page.evaluate(
      (target) =>
        new Promise<void>((resolve, reject) => {
          const socket = new WebSocket(target)
          const record = globalThis as unknown as { closeCode?: number }
          socket.onopen = () => resolve()
          socket.onclose = (event) => {
            record.closeCode = event.code
            reject(new Error(`closed ${event.code} before it opened`))
          }
        }),
      url,
    )
    const other = await browser.newContext({ ignoreHTTPSErrors: true })
    try {
      const second = await other.newPage()
      await signIn(second, '/settings/system#signed-in-sessions')
      await second.getByRole('button', { name: 'Sign out everywhere' }).click()
      await expect
        .poll(() => page.evaluate(() => (globalThis as unknown as { closeCode?: number }).closeCode), { timeout: 15_000 })
        .toBe(4401)
      // The page is kept under the sign-in dialog rather than reconnecting signed out.
      await expect(page.getByRole('alertdialog', { name: 'Sign in to continue' })).toBeVisible({ timeout: 15_000 })
    } finally {
      await other.close()
    }
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
