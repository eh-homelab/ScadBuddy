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

// #3777 — laid out, not only classed. The page slot's read-only view of inputs that would
// not migrate keeps three lines of them visible however long the error, counted inside
// the textarea's padding (#3844 review: a border-box floor of 3lh shows about two), and
// where 30vh holds that floor, the textarea stays inside its alert.
async function openUnmigrated(page: Page, height: number) {
  await page.setViewportSize({ width: 1280, height })
  await page.goto('/m/ui-unmigrated')
  await choose(page, page.getByRole('combobox', { name: 'Preset' }), 'Old')
  const saved = page.getByRole('textbox', { name: 'Saved inputs' })
  await expect(saved).toBeVisible()
  const alert = page.getByRole('alert').filter({ has: saved })
  await expect(alert).toContainText('defines no migrate')
  return { saved, alert }
}

test('keeps three lines of unmigrated inputs visible in a short window', async ({ page }) => {
  const { saved } = await openUnmigrated(page, 300)
  const lines = await saved.evaluate((el) => {
    const style = el.ownerDocument.defaultView!.getComputedStyle(el)
    const text = el.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)
    return text / parseFloat(style.lineHeight)
  })
  expect(lines).toBeGreaterThanOrEqual(2.98)
})

test('keeps the unmigrated inputs inside their alert where 30vh holds them', async ({ page }) => {
  const { saved, alert } = await openUnmigrated(page, 420)
  const box = (await alert.boundingBox())!
  // The precondition first (#3893 review): the alert is at its 30vh cap, so an overflow
  // below is the textarea's, not a cap that moved.
  expect(box.height).toBeCloseTo(0.3 * 420, 0)
  const area = (await saved.boundingBox())!
  expect(area.y + area.height).toBeLessThanOrEqual(box.y + box.height + 0.5)
})
