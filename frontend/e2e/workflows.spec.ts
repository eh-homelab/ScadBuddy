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
// forgetting a run, on a laptop and a phone. #2487 — the script editor: an error by
// line, Save as a new version, a diff of two versions and Restore; on a phone the
// script and the diff are read only.
for (const { name, viewport } of [
  { name: '1440×900', viewport: { width: 1440, height: 900 } },
  { name: '390×844', viewport: { width: 390, height: 844 } },
]) {
  test.describe(`flows at ${name} (#2350)`, () => {
    test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')
    test.use({ viewport })

    const phone = viewport.width < 640
    const noSideScroll = async (page: Page) =>
      expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(viewport.width)

    test('lists a flow, shows its script, and starts a run of it', async ({ page }) => {
      await page.goto('/workflows')
      await page.getByRole('navigation', { name: 'Workflows' }).getByRole('link', { name: 'Flows' }).click()
      await expect(page).toHaveURL(/\/workflows\/flows$/)
      const plates = page.locator('[data-flow="plates"]')
      await expect(plates).toContainText('latest v2')
      await expect(plates).toContainText('Registered by you, in the browser')
      await noSideScroll(page)

      await page.locator('[data-flow="swap"]').getByRole('link', { name: 'swap' }).click()
      await expect(page).toHaveURL(/\/workflows\/flows\/def-swap$/)
      if (phone) {
        // #2487 — read only on a phone: highlighted text, no editor.
        const script = page.getByLabel('Script', { exact: true })
        await expect(script).toContainText('await wait_for_human("Swap to pink?", 600)')
        await expect(script.locator('[data-token="keyword"]').first()).toHaveText('async')
        await expect(script).toBeInViewport()
      } else {
        const script = page.locator('.monaco-editor .view-lines')
        await expect(script).toContainText('await wait_for_human("Swap to pink?", 600)')
        await expect(script).toBeInViewport()
      }
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

    test('edits a script, sees an error by line, saves a new version, diffs two versions and restores one', async ({
      page,
    }) => {
      test.skip(phone, 'a phone reads the script; the next test')
      await page.goto('/workflows/flows/def-swap')
      const lines = page.locator('.monaco-editor .view-lines')
      // The editor is Monaco's lazy chunk, fetched on this first visit.
      await expect(lines).toContainText('await wait_for_human("Swap to pink?", 600)', { timeout: 15_000 })
      // A new line 3, under the comment: Monaco keeps the indent and closes the brackets.
      await lines.click()
      await expect(page.getByRole('textbox', { name: /^Script\./ })).toBeFocused()
      await page.keyboard.press('ControlOrMeta+Home')
      await page.keyboard.press('ArrowDown')
      await page.keyboard.press('End')
      await page.keyboard.press('Enter')
      await page.keyboard.type('await sleep("soon"')
      const problems = page.getByRole('list', { name: 'Problems' })
      await expect(problems.getByRole('listitem')).toHaveText([/^Line 3.*sleep/])
      await expect(page.locator('.monaco-editor .squiggly-error').first()).toBeVisible()
      await noSideScroll(page)

      await page.keyboard.press('Home')
      await page.keyboard.press('Shift+End')
      await page.keyboard.type('await sleep(5')
      await expect(lines).toContainText('await sleep(5)')
      await expect(page.getByRole('status')).toHaveText('No problems')
      await expect(problems).toHaveCount(0)
      await page.getByRole('button', { name: 'Save' }).click()
      await expect(page).toHaveURL(/\/workflows\/flows\/def-swap-2$/)
      await expect(page.getByRole('heading', { name: 'swap v2' })).toBeVisible()
      const versions = page.getByRole('region', { name: 'Versions' })
      await expect(versions.getByRole('listitem').first()).toContainText('v2 you, in the browser')

      const compare = page.getByRole('region', { name: 'Compare versions' })
      await expect(compare.getByRole('combobox', { name: 'From' })).toHaveValue(/^c0ffee1/)
      await compare.getByRole('button', { name: 'Compare' }).click()
      await expect(compare.getByRole('heading', { name: 'v1 → v2' })).toBeVisible()
      const diff = page.locator('.monaco-diff-editor')
      await expect(diff.locator('.line-insert').first()).toBeVisible()
      await expect(diff.locator('.modified-in-monaco-diff-editor .view-lines')).toContainText('await sleep(5)')
      await noSideScroll(page)

      await versions.getByRole('link', { name: 'v1' }).click()
      await expect(page).toHaveURL(/\/workflows\/flows\/def-swap$/)
      await page.getByRole('button', { name: 'Restore v1' }).click()
      await expect(page).toHaveURL(/\/workflows\/flows\/def-swap-3$/)
      await expect(page.getByRole('heading', { name: 'swap v3' })).toBeVisible()
      await expect(lines).toContainText('await wait_for_human("Swap to pink?", 600)')
      await expect(lines).not.toContainText('sleep(5)')
      await expect(page.getByRole('button', { name: /^Restore/ })).toHaveCount(0)
    })

    test('on a phone, reads a script and diffs two versions, with nothing to edit', async ({ page }) => {
      test.skip(!phone, 'the laptop edits; the test before')
      await page.goto('/workflows/flows/def-plates-1')
      const script = page.getByLabel('Script', { exact: true })
      await expect(script).toContainText('return 1')
      await expect(page.locator('.monaco-editor')).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Save' })).toHaveCount(0)
      await expect(page.getByRole('button', { name: /^Restore/ })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Start run' })).toBeVisible()

      const compare = page.getByRole('region', { name: 'Compare versions' })
      await compare.getByRole('combobox', { name: 'To' }).selectOption({ label: 'v2' })
      await compare.getByRole('button', { name: 'Compare' }).click()
      await expect(compare.getByRole('heading', { name: 'v1 → v2' })).toBeVisible()
      const diff = page.locator('.monaco-diff-editor')
      await expect(diff).toBeVisible()
      await expect(diff).toContainText('queue_print')
      await noSideScroll(page)
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
