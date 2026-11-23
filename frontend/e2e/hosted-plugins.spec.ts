import { expect, test } from '@playwright/test'

/**
 * #6937 — Settings → Tools & plugins → Hosted plugins (spec 2026-11-21 §6.2, §6.3), against
 * the msw stand-ins for the backend's `/api/v1/hosted-plugins` (`src/mocks/features/hostedPlugins.ts`)
 * and the agent's package install (`src/mocks/aiPlugins.ts`): create a plugin, add a skill and
 * edit it in Monaco, and publish, which offers the version; Packages then lists for approval.
 */
test.describe('hosted plugins in Settings (#6937)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the agent service is not in the real stack yet')

  test('creates, edits, publishes and offers a plugin, and Packages lists it for approval', async ({ page }) => {
    await page.goto('/settings/tools#hosted-plugins')
    const section = page.getByRole('region', { name: 'Your plugins' })
    // The seeded plugin was published and never offered.
    await expect(section.getByRole('listitem', { name: 'Hosted plugin print-notes' }).getByText('Version 1 not offered')).toBeVisible()

    await section.getByLabel('Name').fill('wave-helper')
    await section.locator('#hosted-create-description').fill('Waves at the user')
    await section.getByRole('button', { name: 'Create plugin' }).click()
    const editor = section.getByRole('region', { name: 'Hosted plugin wave-helper' })
    await expect(editor.getByText('Not published yet')).toBeVisible()

    // A path outside the folders is refused before it is sent.
    await editor.getByLabel('New file').fill('README.md')
    await editor.getByRole('button', { name: 'Create empty' }).click()
    await expect(editor.getByText('A file must be under skills/, agents/ or commands/.')).toBeVisible()

    await editor.getByLabel('New skill').fill('wave')
    await editor.getByRole('button', { name: 'Add' }).click()
    await expect(editor.getByRole('button', { name: 'skills/wave/SKILL.md' })).toHaveAttribute('aria-current', 'true')
    const lines = editor.locator('.monaco-editor .view-lines')
    await expect(lines).toContainText('name: wave')
    await lines.click()
    await page.keyboard.press('Control+End')
    await page.keyboard.type('Wave twice.')
    await expect(editor.getByText('Unsaved', { exact: true })).toBeVisible()
    await expect(editor.getByRole('button', { name: 'Publish version 1' })).toBeDisabled()
    await page.keyboard.press('Control+s')
    await expect(editor.getByText('Unsaved', { exact: true })).toBeHidden()

    const publish = editor.getByRole('region', { name: 'Publish' })
    await expect(publish.getByRole('list', { name: 'Draft changes' })).toContainText('skills/wave/SKILL.md')
    await publish.getByRole('button', { name: 'Publish version 1' }).click()
    await expect(publish.getByText('Published version 1.')).toBeVisible()
    // Publish offers the version at once (spec §6.3 step 5); nothing approves it.
    await expect(publish.getByText(/Offered version 1\. It is installed and waits for your approval\./)).toBeVisible()
    await expect(editor.getByRole('region', { name: 'Versions' }).getByText('Version 1', { exact: true })).toBeVisible()

    const assistant = editor.getByRole('region', { name: 'Assistant' })
    await expect(assistant.getByText('Version 1 is installed and waits for your approval.')).toBeVisible()
    await expect(assistant.getByRole('button', { name: 'Offer version 1' })).toHaveCount(0)

    // Packages, on the same page, reads the packages again and lists the install for approval.
    await publish.getByRole('link', { name: 'Review version 1 in Packages' }).click()
    const card = page.getByRole('region', { name: 'Plugin packages' }).getByRole('listitem', { name: 'Plugin package wave-helper' })
    await expect(card.getByText('Awaiting approval')).toBeVisible()
    await expect(card.getByText('Hosted plugin wave-helper')).toBeVisible()
  })
})
