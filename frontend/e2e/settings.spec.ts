import { expect, test } from '@playwright/test'

// #322 — a settings round-trip against the msw API: a value saved in one section
// keeps its source across navigation, a reset puts the deployment's value back, and
// unsaved edits hold an in-app link until the user decides.

test.describe('settings', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('saves a section, keeps it across navigation, and resets it to the deployment', async ({ page }) => {
    await page.goto('/settings/library')
    const rendering = page.getByRole('region', { name: 'Rendering' })
    // By role: the reset button's name also says "Render timeout" (#2346, label in name).
    const timeout = page.getByRole('spinbutton', { name: 'Render timeout' })
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
    await expect(page).toHaveURL(/\/settings\/general$/)
    await page.getByRole('navigation', { name: 'Settings areas' }).getByRole('link', { name: 'Library', exact: true }).click()
    await expect(timeout).toHaveValue('45')
    await expect(source).toHaveText(/Set here/)

    await source.getByRole('button', { name: 'Reset to deployment value: Render timeout' }).click()
    await expect(source).toHaveText(/From SCADBUDDY_RENDER_TIMEOUT/)
    await expect(timeout).toHaveValue('300')
  })

  test('holds an in-app link while a section is unsaved, and discards on request', async ({ page }) => {
    await page.goto('/settings/library')
    const preview = page.getByRole('region', { name: 'Preview' })
    await page.getByLabel('Show dimensions in').click()
    await page.getByRole('option', { name: 'Inches (in)' }).click()
    await expect(preview.getByText('Unsaved')).toBeVisible()

    await page.getByRole('link', { name: 'Models', exact: true }).click()
    const dialog = page.getByRole('alertdialog', { name: 'Leave without saving?' })
    await expect(dialog).toContainText('Preview')
    await dialog.getByRole('button', { name: 'Stay' }).click()
    await expect(page).toHaveURL(/\/settings\/library$/)

    await preview.getByRole('button', { name: 'Discard Preview changes' }).click()
    await expect(page.getByLabel('Show dimensions in')).toHaveText('Millimetres (mm)')
    await page.getByRole('link', { name: 'Models', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    // The catalogue writes its view to the URL (#534, lib/catalogueQuery.ts).
    await expect(page).toHaveURL(/\/(\?view=cards)?$/)
  })

  // #4840 — switching areas while a section is unsaved keeps the leave guard's sentinel
  // on top: Back walks the areas, Forward never bounces, and once saved one Back per area
  // leaves Settings.
  test('switches areas while unsaved, then Back walks them and saving leaves no stray entry', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'Settings', exact: true }).click()
    await expect(page).toHaveURL(/\/settings\/general$/)
    const areas = page.getByRole('navigation', { name: 'Settings areas' })
    await areas.getByRole('link', { name: 'Library', exact: true }).click()
    const rendering = page.getByRole('region', { name: 'Rendering' })
    const timeout = page.getByRole('spinbutton', { name: 'Render timeout' })
    await expect(timeout).toHaveValue('300')
    await timeout.fill('45')
    await expect(rendering.getByText('Unsaved')).toBeVisible()

    const dialog = page.getByRole('alertdialog', { name: 'Leave without saving?' })
    await areas.getByRole('link', { name: 'Printing', exact: true }).click()
    await expect(page).toHaveURL(/\/settings\/printing$/)
    await page.goBack()
    await expect(page).toHaveURL(/\/settings\/library$/)
    await expect(dialog).toHaveCount(0)
    await page.goForward()
    await expect(page).toHaveURL(/\/settings\/library$/)
    await expect(dialog).toHaveCount(0)
    await expect(timeout).toHaveValue('45')

    await rendering.getByRole('button', { name: 'Save Rendering' }).click()
    await expect(rendering.getByText('Unsaved')).toHaveCount(0)
    await page.goBack()
    await expect(page).toHaveURL(/\/settings\/general$/)
    await page.goBack()
    await expect(page).toHaveURL(/\/(\?view=cards)?$/)
  })

  // #969 — at desktop width the section nav was capped at max-w-2xl with a hidden
  // horizontal scrollbar, so the last four sections sat off-screen with no affordance.
  // The last section the mock renders for each area (hideWhenEmpty sections included, as
  // the mock backend has what they need).
  const LAST_SECTION = {
    general: 'Connection',
    printing: 'Hotend usage',
    library: 'Preview',
    assistant: 'Memory',
    tools: 'Remote MCP plugins',
    mcp: 'OIDC sign-in',
    audits: 'Audit log',
    system: 'About',
  } as const
  for (const [area, last] of Object.entries(LAST_SECTION)) {
    test(`shows every area link, and the ${area} area’s section links, at desktop width`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(`/settings/${area}`)
      for (const [label, lastName] of [
        ['Settings areas', 'System'],
        ['Settings sections', last],
      ] as const) {
        const nav = page.getByRole('navigation', { name: label })
        const links = nav.getByRole('link')
        await expect(links.last()).toHaveText(lastName)
        if (label === 'Settings areas') await expect(links).toHaveCount(8)
        const navBox = (await nav.boundingBox())!
        for (const link of await links.all()) {
          const box = (await link.boundingBox())!
          const name = await link.textContent()
          expect(box.x, `${name} starts inside the ${label} nav`).toBeGreaterThanOrEqual(navBox.x)
          expect(box.x + box.width, `${name} ends inside the ${label} nav`).toBeLessThanOrEqual(navBox.x + navBox.width)
        }
        const list = nav.getByRole('list')
        const overflow = await list.evaluate((el) => el.scrollWidth - el.clientWidth)
        expect(overflow, `the ${label} nav does not scroll sideways`).toBeLessThanOrEqual(0)
      }
    })
  }

  test('moves between areas with a draft, and asks only when leaving Settings', async ({ page }) => {
    await page.goto('/settings/library')
    const timeout = page.getByRole('spinbutton', { name: 'Render timeout' })
    await timeout.fill('45')
    await expect(page.getByRole('region', { name: 'Rendering' }).getByText('Unsaved')).toBeVisible()

    await page.getByRole('navigation', { name: 'Settings areas' }).getByRole('link', { name: 'General', exact: true }).click()
    await expect(page).toHaveURL(/\/settings\/general$/)
    await expect(page.getByRole('alertdialog')).toHaveCount(0)

    await page.getByRole('navigation', { name: 'Settings areas' }).getByRole('link', { name: /^Library/ }).click()
    await expect(timeout).toHaveValue('45')

    await page.getByRole('link', { name: 'Models', exact: true }).click()
    await expect(page.getByRole('alertdialog', { name: 'Leave without saving?' })).toContainText('Rendering')
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
      await expect(page).toHaveURL(/\/settings\/system#about$/)
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
