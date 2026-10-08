import { expect, test } from '@playwright/test'

// #322 — a settings round-trip against the msw API: a value saved in one section
// keeps its source across navigation, a reset puts the deployment's value back, and
// unsaved edits hold an in-app link until the user decides.

test.describe('settings', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('saves a section, keeps it across navigation, and resets it to the deployment', async ({ page }) => {
    await page.goto('/settings')
    const rendering = page.getByRole('region', { name: 'Rendering' })
    const timeout = page.getByLabel('Render timeout')
    const source = page.getByTestId('source-render_timeout')
    await expect(timeout).toHaveValue('300')
    await expect(source).toHaveText(/From SCADBUDDY_RENDER_TIMEOUT/)

    await timeout.fill('45')
    await expect(rendering.getByText('Unsaved')).toBeVisible()
    await rendering.getByRole('button', { name: 'Save Rendering' }).click()
    await expect(rendering.getByText('Unsaved')).toHaveCount(0)
    await expect(source).toHaveText(/Set here/)

    // Away and back inside the app: the mock backend holds what was saved.
    await page.getByRole('link', { name: 'Models', exact: true }).click()
    // The catalogue writes its view to the URL (#534, lib/catalogueQuery.ts).
    await expect(page).toHaveURL(/\/(\?view=cards)?$/)
    await page.getByRole('link', { name: 'Settings', exact: true }).click()
    await expect(timeout).toHaveValue('45')
    await expect(source).toHaveText(/Set here/)

    await source.getByRole('button', { name: /Reset render_timeout/ }).click()
    await expect(source).toHaveText(/From SCADBUDDY_RENDER_TIMEOUT/)
    await expect(timeout).toHaveValue('300')
  })

  test('holds an in-app link while a section is unsaved, and discards on request', async ({ page }) => {
    await page.goto('/settings')
    const preview = page.getByRole('region', { name: 'Preview' })
    await page.getByLabel('Show dimensions in').selectOption('in')
    await expect(preview.getByText('Unsaved')).toBeVisible()

    await page.getByRole('link', { name: 'Models', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Leave without saving?' })
    await expect(dialog).toContainText('Preview')
    await dialog.getByRole('button', { name: 'Stay' }).click()
    await expect(page).toHaveURL(/\/settings$/)

    await preview.getByRole('button', { name: 'Discard Preview changes' }).click()
    await expect(page.getByLabel('Show dimensions in')).toHaveValue('mm')
    await page.getByRole('link', { name: 'Models', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    // The catalogue writes its view to the URL (#534, lib/catalogueQuery.ts).
    await expect(page).toHaveURL(/\/(\?view=cards)?$/)
  })

  // #969 — at desktop width the section nav was capped at max-w-2xl with a hidden
  // horizontal scrollbar, so the last four sections sat off-screen with no affordance.
  test('shows every section link at desktop width', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/settings')
    const nav = page.getByRole('navigation', { name: 'Settings sections' })
    const links = nav.getByRole('link')
    await expect(links.last()).toHaveText(/About/)
    const navBox = (await nav.boundingBox())!
    for (const link of await links.all()) {
      const box = (await link.boundingBox())!
      const name = await link.textContent()
      expect(box.x, `${name} starts inside the nav`).toBeGreaterThanOrEqual(navBox.x)
      expect(box.x + box.width, `${name} ends inside the nav`).toBeLessThanOrEqual(navBox.x + navBox.width)
    }
    const list = nav.getByRole('list')
    const overflow = await list.evaluate((el) => el.scrollWidth - el.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)
  })
})

// #1035 — the About section's deployment-values table held unbroken tokens (a comma-joined
// origin list, long variable names), so it ran past the card at every width and panned the
// whole page sideways below the desktop.
test.describe('About at every width (#1035)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  for (const [width, height] of [
    [1440, 900],
    [768, 1024],
    [390, 844],
  ] as const) {
    test(`the deployment values stay inside the About card at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await page.goto('/settings#about')
      const table = page.getByRole('table', { name: 'Deployment values' })
      await expect(table).toContainText('SCADBUDDY_ALLOWED_ORIGINS')
      const about = page.getByRole('region', { name: 'About' })
      const card = (await about.boundingBox())!
      const box = (await table.boundingBox())!
      expect(box.x + box.width).toBeLessThanOrEqual(card.x + card.width + 0.5)
      // The page's own scroller, which CSS lets scroll sideways once anything is too wide.
      const sideways = await page.evaluate(`[...document.querySelectorAll('[class~="overflow-y-auto"]')]
        .filter((el) => el.scrollWidth > el.clientWidth + 1)
        .map((el) => el.className + ' ' + el.scrollWidth + '>' + el.clientWidth)`)
      expect(sideways).toEqual([])
    })
  }
})
