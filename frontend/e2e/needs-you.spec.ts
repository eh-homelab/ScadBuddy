import { expect, test, type Page } from '@playwright/test'

// #6399 — every request that waits on the person: the bell lists it with its answer, and the
// Activity page's "Needs you" lists it in full. The mocks hold an agent's requested run grant
// (`run-ask`) and a requested standing grant (`trig-render`); each test parks one prepared
// action (an MCP client's, `src/mocks/features/pendingInput.ts` `PENDING_INPUT_SEED_KEY`).
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

async function parkOneApproval(page: Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem('scadbuddy.mock.pending-input', JSON.stringify({ approvals: 1 }))
  })
}

const bell = (page: Page) => page.getByRole('button', { name: /^Activity, \d+ needs? you/ })

test('the bell lists what waits, and Activity answers it', async ({ page }) => {
  await parkOneApproval(page)
  await page.goto('/')
  await expect(bell(page)).toHaveAccessibleName('Activity, 3 need you')
  await expect(page).toHaveTitle(/^\(3\) /)

  // The dropdown: the waiting requests first, each with its answer, and a way to all of them.
  await bell(page).click()
  const dropdown = page.getByRole('region', { name: 'Needs you' })
  const rows = dropdown.getByTestId('needs-you-item')
  await expect(rows).toHaveCount(3)
  await expect(rows.filter({ has: page.getByRole('heading', { name: 'Prepared action' }) }).getByRole('button', { name: 'Approve' })).toBeVisible()
  await expect(rows.filter({ has: page.getByRole('heading', { name: 'Grant request', exact: true }) }).getByRole('link', { name: 'Review' })).toBeVisible()
  await expect(page.getByText('Finished since you last looked')).toBeVisible()
  await dropdown.getByRole('link', { name: 'See all' }).click()

  // The full list, at the top of Activity.
  await expect(page).toHaveURL(/\/activity#needs-you$/)
  const section = page.getByRole('main').getByRole('region', { name: /^Needs you/ })
  await expect(section.getByRole('heading', { name: /^Needs you/ })).toBeFocused()
  const items = section.getByTestId('needs-you-item')
  await expect(items).toHaveCount(3)
  const grant = items.filter({ has: page.getByRole('heading', { name: 'Grant request', exact: true }) })
  await expect(grant).toContainText('Flow run swap')
  await grant.getByRole('button', { name: 'Review grant…' }).click()
  await expect(grant.getByRole('table', { name: 'Grant rules' })).toBeVisible()

  // Answered here, it leaves the list and the count.
  await items.filter({ has: page.getByRole('heading', { name: 'Prepared action' }) }).getByRole('button', { name: 'Approve' }).click()
  await expect(items).toHaveCount(2)
  await expect(bell(page)).toHaveAccessibleName('Activity, 2 need you')
  await grant.getByRole('button', { name: 'Decline', exact: true }).click()
  await expect(items).toHaveCount(1)
  await expect(bell(page)).toHaveAccessibleName('Activity, 1 needs you')
})

test('the bell answers from the dropdown, and fits a phone', async ({ page }) => {
  await parkOneApproval(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/workflows')
  await expect(bell(page)).toBeVisible()
  await bell(page).click()
  const dropdown = page.getByRole('region', { name: 'Needs you' })
  await dropdown
    .getByTestId('needs-you-item')
    .filter({ has: page.getByRole('heading', { name: 'Prepared action' }) })
    .getByRole('button', { name: 'Deny' })
    .click()
  await expect(bell(page)).toHaveAccessibleName('Activity, 2 need you')
  expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
})
