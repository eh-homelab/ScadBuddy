import { expect, test, type Page } from '@playwright/test'
import { choose } from './select'

// The ui-demo and ui-broken templates are msw fixtures (src/mocks/templateUi.ts); the real
// stack has neither.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no ui-demo template')

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

test('maze-puzzle shows the lid colour only with a lid', async ({ page }) => {
  await trackCsp(page)
  await page.goto('/m/builtin%3Amaze-puzzle')
  const ui = page.getByTestId('template-ui')
  await expect(ui.locator('sb-param[name="wall_color"]')).toBeVisible()
  await expect(ui.locator('sb-param[name="lid_color"]')).toBeHidden()
  // #3672 — a Radix select in the template's shadow root; its list portals to the page.
  await choose(page, ui.locator('sb-param[name="mode"]').getByRole('combobox'), 'Ball with snap-on lid')
  await expect(ui.locator('sb-param[name="lid_color"]')).toBeVisible()
  await expect(page.getByTestId('ui-origin')).toHaveText('Custom interface · built-in')
  expect(await violations(page)).toEqual([])
})

// #3777 — laid out, not only classed: in a short window the page slot's read-only view of
// inputs that would not migrate keeps its textarea inside the 30vh alert, and at three
// lines at least however long the error.
test('keeps the unmigrated inputs inside their alert, three lines tall, in a short window', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 300 })
  await page.goto('/m/ui-unmigrated')
  await choose(page, page.getByRole('combobox', { name: 'Preset' }), 'Old')

  const saved = page.getByRole('textbox', { name: 'Saved inputs' })
  await expect(saved).toBeVisible()
  const alert = page.getByRole('alert').filter({ has: saved })
  await expect(alert).toContainText('defines no migrate')

  const box = (await alert.boundingBox())!
  const area = (await saved.boundingBox())!
  expect(area.y + area.height).toBeLessThanOrEqual(box.y + box.height + 0.5)
  const line = await saved.evaluate((el) =>
    parseFloat(el.ownerDocument.defaultView!.getComputedStyle(el).lineHeight),
  )
  expect(area.height).toBeGreaterThanOrEqual(3 * line - 0.5)
})
