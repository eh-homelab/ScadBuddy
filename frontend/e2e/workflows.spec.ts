import { expect, test, type Page } from '@playwright/test'

// #1057 — the Workflows page over the msw flow runs (src/mocks/features/flows.ts).

test.describe('workflows (#1057)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('the nav opens the runs, and an answered run moves on', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Workflows' }).click()
    await expect(page).toHaveURL(/\/workflows$/)
    const runs = page.getByRole('list', { name: 'Flow runs' })
    await expect(runs.locator('[data-run]')).toHaveCount(3)
    await expect(page.locator('[data-run="run-ask"]')).toContainText('Waiting for your answer: Swap to pink?')

    await page.locator('[data-run="run-ask"]').getByRole('link', { name: 'swap v1' }).click()
    await expect(page).toHaveURL(/\/workflows\/runs\/run-ask$/)
    await page.getByRole('textbox', { name: 'Your answer' }).fill('yes')
    await page.getByRole('button', { name: 'Answer' }).click()
    await expect(page.getByTestId('flow-run-status')).toHaveText('Running')
    await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveCount(0)
  })
})

// #2350 — the Flows tab, a flow's script and Start run, a run's reset preview, and
// forgetting a run, on a laptop and a phone.
for (const { name, viewport } of [
  { name: '1440×900', viewport: { width: 1440, height: 900 } },
  { name: '390×844', viewport: { width: 390, height: 844 } },
]) {
  test.describe(`flows at ${name} (#2350)`, () => {
    test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')
    test.use({ viewport })

    const noSideScroll = async (page: Page) =>
      expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(viewport.width)

    test('lists a flow, shows its script, and starts a run of it', async ({ page }) => {
      await page.goto('/workflows')
      await page.getByRole('navigation', { name: 'Workflows' }).getByRole('link', { name: 'Flows' }).click()
      await expect(page).toHaveURL(/\/workflows\/flows$/)
      const plates = page.locator('[data-flow="plates"]')
      await expect(plates).toContainText('v2 · 2 versions')
      await expect(plates).toContainText('Registered by you, in the browser')
      await noSideScroll(page)

      await page.locator('[data-flow="swap"]').getByRole('link', { name: 'swap' }).click()
      await expect(page).toHaveURL(/\/workflows\/flows\/def-swap$/)
      const script = page.getByLabel('Script', { exact: true })
      await expect(script).toContainText('await wait_for_human("Swap to pink?", 600)')
      await expect(script.locator('[data-token="keyword"]').first()).toHaveText('async')
      await expect(script).toBeInViewport()
      await noSideScroll(page)

      await page.getByRole('button', { name: 'Start run' }).click()
      await expect(page).toHaveURL(/\/workflows\/runs\/run-new-1$/)
      await expect(page.getByTestId('flow-run-status')).toHaveText('Running')
      await expect(page.getByRole('link', { name: 'swap v1' })).toHaveAttribute('href', '/workflows/flows/def-swap')
      await expect(page.getByLabel('Script', { exact: true })).toContainText('wait_for_human')
    })

    test('previews a reset with the outward call flagged, and resets once confirmed', async ({ page }) => {
      await page.goto('/workflows/runs/run-done')
      await page.getByRole('button', { name: 'Preview a reset' }).click()
      await page.getByRole('combobox', { name: 'Reset point' }).selectOption({ label: 'Event 11: before step 1 (render)' })
      const again = page.getByRole('list', { name: 'Calls that run again' })
      await expect(again.getByRole('listitem')).toHaveCount(3)
      await expect(again.locator('[data-outward]')).toHaveText(/queue_print\s*Outward: sent again/)
      await noSideScroll(page)

      await page.getByRole('button', { name: 'Reset to this point…' }).click()
      const dialog = page.getByRole('dialog', { name: 'Reset this run?' })
      await expect(dialog.getByRole('list', { name: 'Outward calls sent again' })).toContainText('#2 queue_print')
      await expect(dialog.getByRole('button', { name: 'Reset run' })).toBeInViewport()
      await dialog.getByRole('button', { name: 'Reset run' }).click()
      await expect(page.getByText('Reset. The run continues from that point.')).toBeVisible()
      await expect(page.getByTestId('flow-run-status')).toHaveText('Running')
    })

    test('forgets a run once confirmed', async ({ page }) => {
      await page.goto('/workflows/runs/run-done')
      await page.getByRole('button', { name: 'Forget run' }).click()
      const dialog = page.getByRole('dialog', { name: 'Forget this run?' })
      await expect(dialog).toContainText("This can't be undone.")
      await expect(dialog.getByRole('button', { name: 'Forget run' })).toBeInViewport()
      await dialog.getByRole('button', { name: 'Forget run' }).click()
      await expect(page).toHaveURL(/\/workflows$/)
      await expect(page.locator('[data-run]')).toHaveCount(2)
      await expect(page.locator('[data-run="run-done"]')).toHaveCount(0)
    })
  })
}
