import { expect, test, type Page } from '@playwright/test'

/** The browser globals these checks touch; the e2e tsconfig has no DOM lib. */
interface CspWindow {
  __csp: string[]
  document: {
    addEventListener(
      type: 'securitypolicyviolation',
      listener: (event: { violatedDirective: string; blockedURI: string }) => void,
    ): void
  }
}

async function trackCsp(page: Page) {
  await page.addInitScript(() => {
    const win = globalThis as unknown as CspWindow
    const seen: string[] = []
    win.__csp = seen
    win.document.addEventListener('securitypolicyviolation', (event) => {
      seen.push(`${event.violatedDirective} ${event.blockedURI}`)
    })
  })
}

async function violations(page: Page): Promise<string[]> {
  return page.evaluate(() => (globalThis as unknown as CspWindow).__csp)
}

test('a template UI mounts, binds a parameter and records its own state', async ({ page }) => {
  await trackCsp(page)
  await page.goto('/m/ui-demo')
  await expect(page.getByTestId('ui-demo-greeting')).toHaveText('Hello from the template')
  await expect(page.getByTestId('ui-origin')).toContainText('Custom interface')
  // The POST that carries the filled value: "Remember me" alone starts no render.
  const render = page.waitForRequest(
    (request) =>
      request.method() === 'POST' &&
      request.url().endsWith('/models/ui-demo/render') &&
      (request.postDataJSON() as { inputs?: { params?: { name?: string } } }).inputs?.params?.name === 'Zed',
  )
  await page.getByRole('button', { name: 'Remember me' }).click()
  await page.getByTestId('template-ui').getByRole('textbox').first().fill('Zed')
  const body = (await render).postDataJSON() as { inputs: { params: { name: string }; demo?: unknown } }
  expect(body.inputs.params.name).toBe('Zed')
  expect(body.inputs.demo).toEqual({ touched: true })
  expect(await violations(page)).toEqual([])
})

test('a UI that throws leaves the template usable', async ({ page }) => {
  await page.goto('/m/ui-broken')
  await expect(page.getByRole('alert', { name: /template interface/i })).toContainText('broken on purpose')
  await expect(page.getByTestId('generate')).toBeVisible()
})

test('the page CSP breaks nothing the app does', async ({ page }) => {
  await trackCsp(page)
  await page.goto('/')
  await page.goto('/m/name-keychain')
  await expect(page.getByTestId('generate')).toBeVisible()
  // The font picker loads Google Fonts CSS and font files. Offline, the request fails, but
  // that is a network error, never a securitypolicyviolation.
  await page.locator('[data-param="font"]').getByRole('button').first().click()
  await page.waitForLoadState('networkidle')
  expect(await violations(page)).toEqual([])
  // The font picker is a modal dialog over the page; close it before leaving.
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Choose a font' })).toBeHidden()
  await page.getByRole('link', { name: /source/i }).click()
  await page.waitForLoadState('networkidle')
  expect(await violations(page)).toEqual([])
})
