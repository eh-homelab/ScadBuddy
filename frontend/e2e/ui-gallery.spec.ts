import { expect, test, type FrameLocator, type Page } from '@playwright/test'
import { bambuddyFrame } from './bambuddyFrame'

// #3653 — the shadcn components under the page's CSP (vite.config.ts serves PAGE_CSP with
// the preview) and inside Bambuddy's sandboxed frame. The gallery is a mocked-build page.
test.skip(!!process.env.E2E_BASE_URL, 'the gallery is in the mocked build only')

/** Every CSP violation the page or its frames report, from the browser's own event. */
async function recordViolations(page: Page): Promise<string[]> {
  const violations: string[] = []
  page.on('console', (message) => {
    if (/Content Security Policy/i.test(message.text())) violations.push(message.text())
  })
  // A string: e2e/ is typed without the DOM. It runs in every frame, Bambuddy's included.
  await page.addInitScript(`document.addEventListener('securitypolicyviolation', (event) => {
    console.error('Content Security Policy violation: ' + event.violatedDirective + ' ' + event.blockedURI)
  })`)
  return violations
}

/** Open each overlay kind, check it shows, and close it. */
async function openOverlays(page: Page, root: Page | FrameLocator) {
  await root.getByRole('button', { name: 'Dialog', exact: true }).click()
  await expect(root.getByRole('dialog', { name: 'Print this plate?' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(root.getByRole('dialog')).toHaveCount(0)

  await root.getByRole('button', { name: 'Popover' }).click()
  await expect(root.getByLabel('Width')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(root.getByLabel('Width')).toHaveCount(0)

  await root.getByRole('button', { name: 'Dropdown menu' }).click()
  await expect(root.getByRole('menu')).toBeVisible()
  await expect(root.getByRole('menuitem', { name: 'Duplicate' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(root.getByRole('menu')).toHaveCount(0)

  await root.getByRole('button', { name: 'Tooltip' }).hover()
  await expect(root.getByRole('tooltip')).toHaveText('Generate the model')

  await root.getByRole('combobox', { name: 'Plate' }).click()
  await expect(root.getByRole('option', { name: 'Smooth PEI' })).toBeVisible()
  await root.getByRole('option', { name: 'Smooth PEI' }).click()
  await expect(root.getByRole('combobox', { name: 'Plate' })).toHaveText('Smooth PEI')
}

for (const colorScheme of ['dark', 'light'] as const) {
  test.describe(`${colorScheme}`, () => {
    test.use({ colorScheme })

    test('every overlay opens under the page CSP, with no violation', async ({ page }) => {
      const violations = await recordViolations(page)
      await page.goto('/ui-gallery')
      await expect(page.getByRole('heading', { name: 'Components' })).toBeVisible()
      await openOverlays(page, page)
      expect(violations).toEqual([])
    })
  })
}

test('the overlays portal inside Bambuddy’s sandboxed frame', async ({ page, baseURL }) => {
  const violations = await recordViolations(page)
  const frame = await bambuddyFrame(page, baseURL, '/ui-gallery')
  await expect(frame.getByRole('heading', { name: 'Components' })).toBeVisible()
  await openOverlays(page, frame)
  expect(violations).toEqual([])
})
