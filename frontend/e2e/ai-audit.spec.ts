import { expect, test } from '@playwright/test'

test.describe('AI activity in Settings (#258)', () => {
  // The mocked build serves the agent's audit log from msw (src/mocks/audit.ts);
  // against a real stack the section needs a running agent service.
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')

  test('lists the audit log, filters it, and saves the retention', async ({ page }) => {
    await page.goto('/settings')
    const section = page.getByRole('region', { name: 'AI activity' })
    await expect(section).toBeVisible()

    const entries = section.getByRole('list', { name: 'AI activity entries' }).getByTestId('audit-entry')
    await expect(entries).toHaveCount(8)
    await expect(entries.first()).toContainText('set_print_options')
    await expect(entries.first()).toContainText('approved by You')

    await section.getByLabel('Outcome').selectOption('denied')
    await expect(entries).toHaveCount(2)

    const retention = section.getByLabel('Keep entries for (days)')
    await expect(retention).toHaveValue('90')
    await retention.fill('30')
    await section.getByRole('button', { name: 'Save' }).click()
    await expect(section.getByRole('status')).toHaveText('Entries are kept for 30 days.')
  })
})
