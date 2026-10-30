import { expect, test } from '@playwright/test'
import { bridgeCall } from './bridge'

/**
 * #297 — Settings → Plugin packages and Plugin endpoints, against the msw stand-ins for
 * the agent service (`src/mocks/aiPlugins.ts`). Install from git, review each part,
 * approve the exact commit and hash, switch on, re-pin with a diff, remove; a refused
 * package lists its problems; and the in-page agent cannot approve or install.
 */

const V1 = '3f1c9a2b7d4e5f60718293a4b5c6d7e8f9012345'
const V1_HASH = 'sha256:9b2e6c1f0a3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b'
const V2 = '7a8b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f607'

test.describe('assistant plugins in Settings (#297)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the agent service is not in the real stack yet')

  test('installs, reviews, approves the exact pin, enables, re-pins with a diff and removes', async ({ page }) => {
    await page.goto('/settings/tools')
    const section = page.getByRole('region', { name: 'Plugin packages' })
    await expect(section.getByText('No plugin packages installed.')).toBeVisible()

    // A refused package names each problem.
    await section.getByLabel('Repository URL').fill('https://git.example/reserved.git')
    await section.getByRole('button', { name: 'Fetch and review' }).click()
    const refused = section.getByRole('list', { name: 'Why the package was refused' })
    await expect(refused.getByRole('listitem')).toHaveCount(1)
    await expect(refused).toContainText('is not 1–64')

    // One the rules refuse installs, and lists what an approval would allow.
    await section.getByLabel('Repository URL').fill('https://git.example/shell.git')
    await section.getByRole('button', { name: 'Fetch and review' }).click()
    const shell = section.getByRole('listitem', { name: 'Plugin package shell' })
    const allowable = shell.getByRole('list', { name: 'What the rules refuse in shell' })
    await expect(allowable.getByRole('listitem')).toHaveCount(2)
    await expect(allowable).toContainText('"command" hook')
    await shell.getByRole('button', { name: 'Remove' }).click()
    await page.getByRole('alertdialog', { name: 'Remove shell?' }).getByRole('button', { name: 'Remove package' }).click()
    await expect(shell).toHaveCount(0)

    await section.getByLabel('Repository URL').fill('https://git.example/greeter.git')
    await section.getByRole('button', { name: 'Fetch and review' }).click()
    const card = section.getByRole('listitem', { name: 'Plugin package greeter' })
    await expect(card.getByText('Awaiting approval')).toBeVisible()
    const review = card.getByLabel('Review of greeter')
    await expect(review).toContainText('/greeter:hello')
    await expect(review).toContainText('greeter:helper')
    await expect(review).toContainText('Stop: prompt')
    await expect(review).toContainText('mem (http) https://mcp.example/mcp/')

    await card.getByRole('button', { name: 'Approve…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Approve greeter' })
    await expect(dialog.getByTestId('approve-commit')).toHaveText(V1)
    await expect(dialog.getByTestId('approve-hash')).toHaveText(V1_HASH)
    await expect(dialog.getByRole('button', { name: 'Approve this pin' })).toBeDisabled()
    await dialog.getByRole('checkbox').check()
    await dialog.getByRole('button', { name: 'Approve this pin' }).click()
    await expect(dialog).toBeHidden()

    // The package's own switch (#4757), on its capabilities source.
    await card.getByRole('switch', { name: 'Enabled (greeter)' }).click()
    await expect(card.locator('[data-slot="badge"]', { hasText: /^Enabled$/ })).toBeVisible()

    await card.getByLabel('Re-pin to branch, tag or commit').fill('v2')
    await card.getByRole('button', { name: 'Fetch re-pin' }).click()
    const pending = card.getByLabel('Pending re-pin')
    await expect(pending).toContainText(V2)
    await expect(pending.getByRole('list', { name: 'Files changed by the re-pin' })).toContainText('skills/bye/SKILL.md')
    await expect(card.getByText(V1, { exact: true })).toBeVisible() // still the loaded pin

    await pending.getByRole('button', { name: 'Approve re-pin…' }).click()
    const repin = page.getByRole('dialog', { name: 'Approve the re-pin of greeter' })
    await expect(repin.getByTestId('approve-commit')).toHaveText(V2)
    await repin.getByRole('checkbox').check()
    await repin.getByRole('button', { name: 'Approve this pin' }).click()
    await expect(card.getByText(V2, { exact: true })).toBeVisible()
    await expect(card.locator('[data-slot="badge"]', { hasText: /^Enabled$/ })).toBeVisible()

    await card.getByRole('button', { name: 'Remove' }).click()
    await page.getByRole('alertdialog', { name: 'Remove greeter?' }).getByRole('button', { name: 'Remove package' }).click()
    await expect(section.getByText('No plugin packages installed.')).toBeVisible()
  })

  test('the in-page agent cannot install or approve', async ({ page }) => {
    await page.goto('/settings/tools')
    await page.waitForFunction(() => Boolean((globalThis as { __scadbuddyBridge?: unknown }).__scadbuddyBridge))
    const section = page.getByRole('region', { name: 'Plugin packages' })
    await expect(section.getByText('No plugin packages installed.')).toBeVisible()

    const fill = await bridgeCall(page, 'fill', { label: 'Repository URL', value: 'https://git.example/greeter.git' })
    expect(fill).toMatchObject({ ok: false, error: { code: 'refused' } })
    const install = await bridgeCall(page, 'click', { role: 'button', name: 'Fetch and review' })
    expect(install).toMatchObject({ ok: false, error: { code: 'refused' } })

    // The user installs and opens the approval; the agent still cannot confirm it.
    await section.getByLabel('Repository URL').fill('https://git.example/greeter.git')
    await section.getByRole('button', { name: 'Fetch and review' }).click()
    await section.getByRole('button', { name: 'Approve…' }).click()
    const tick = await bridgeCall(page, 'click', {
      role: 'checkbox',
      name: `I reviewed commit ${V1.slice(0, 12)} and content hash ${V1_HASH.slice(0, 19)}…`,
    })
    expect(tick).toMatchObject({ ok: false, error: { code: 'refused' } })
    const approve = await bridgeCall(page, 'click', { role: 'button', name: 'Approve this pin' })
    expect(approve).toMatchObject({ ok: false, error: { code: 'refused' } })
    // The approval is still open, and closing it leaves the pin unapproved. (The dialog is
    // modal: the page behind it is out of the accessibility tree until it closes.)
    const dialog = page.getByRole('dialog', { name: 'Approve greeter' })
    await expect(dialog).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(section.getByText('Awaiting approval')).toBeVisible()
  })

  test("tests a plugin endpoint and sets its tools' access", async ({ page }) => {
    await page.goto('/settings/tools')
    const section = page.getByRole('region', { name: 'Plugins', exact: true })
    const card = section.getByRole('listitem', { name: 'Plugin endpoint hindsight' })
    await expect(card).toContainText('Authorization: …9f3a')
    await card.getByRole('button', { name: 'Test connection' }).click()
    // The mock lists five tools (`src/mocks/aiPlugins.ts` `toolsFor`).
    await expect(card.getByText(/^connected; 5 tools/)).toBeVisible()

    const source = card.getByRole('region', { name: 'Source hindsight' })
    await source.getByRole('button', { name: 'hindsight', exact: true }).click()

    // The seed's stored tier: shown, with Reset.
    await expect(source.getByRole('combobox', { name: 'Access of recall' })).toHaveText('read')
    await expect(source.getByRole('button', { name: 'Reset recall' })).toBeVisible()

    // Claude Code renames files.list: it takes off or outward only, and is not disabled.
    const files = source.getByRole('combobox', { name: 'Access of files.list' })
    await expect(files).toBeEnabled()
    await files.click()
    await expect(page.getByRole('listbox').getByRole('option')).toHaveText(['off', 'outward (asks first) (default)'])
    await page.keyboard.press('Escape')

    // A colliding pair is fixed off.
    await expect(source.getByRole('combobox', { name: 'Access of notes.get' })).toBeDisabled()
    await expect(source.getByRole('combobox', { name: 'Access of notes_get' })).toBeDisabled()

    // Lowering retain below its default asks one line, and applies at once.
    await source.getByRole('combobox', { name: 'Access of retain' }).click()
    await page.getByRole('listbox').getByRole('option', { name: 'write', exact: true }).click()
    const lower = page.getByRole('alertdialog', { name: 'Lower retain?' })
    await lower.getByRole('button', { name: 'Lower to write' }).click()
    await expect(lower).toHaveCount(0)
    await expect(source.getByRole('combobox', { name: 'Access of retain' })).toHaveText('write')
    await expect(source.getByRole('button', { name: 'Reset retain' })).toBeVisible()

    await source.getByRole('switch', { name: 'Enabled (hindsight)', exact: true }).click()
    await expect(card.locator('[data-slot="badge"]', { hasText: 'Enabled' })).toBeVisible()

    // Stored, not only shown: a reload reads the access and the switch back.
    await page.reload()
    await expect(card.locator('[data-slot="badge"]', { hasText: 'Enabled' })).toBeVisible()
    await source.getByRole('button', { name: 'hindsight', exact: true }).click()
    await expect(source.getByRole('combobox', { name: 'Access of retain' })).toHaveText('write')
    await expect(source.getByRole('combobox', { name: 'Access of recall' })).toHaveText('read')
    await expect(source.getByRole('switch', { name: 'Enabled (hindsight)', exact: true })).toBeChecked()
  })
})
