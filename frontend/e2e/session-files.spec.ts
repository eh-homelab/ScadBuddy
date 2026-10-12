import { expect, test } from '@playwright/test'

// #2329 part 3: the assistant panel's Files, against the msw mock of the agent's
// /api/v1/ai/sessions/:id/files routes (src/mocks/features/sessionFiles.ts): a real
// multipart upload, the list, a download and a confirmed delete.

test.describe('assistant Files (#2329)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')

  test('uploads a file, lists it, downloads it and deletes it', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await composer.fill('Make the name bigger')
    await composer.press('Enter')
    await expect(panel.getByTestId('active-session-header')).toBeVisible()

    await panel.getByRole('button', { name: 'Files' }).click()
    const files = panel.getByRole('region', { name: "This session's files" })
    await expect(files.getByText(/No files yet/)).toBeVisible()

    await files.getByLabel('Upload files to the workspace').setInputFiles({
      name: 'my part.scad',
      mimeType: 'text/plain',
      buffer: Buffer.from('cube(10);\n'),
    })
    const list = files.getByRole('list', { name: 'Workspace files' })
    await expect(list.getByText('uploads/my_part.scad')).toBeVisible()
    await expect(list).toContainText('uploaded by you')
    await expect(composer).toHaveValue('[workspace file uploads/my_part.scad]')

    const download = page.waitForEvent('download')
    await files.getByRole('button', { name: 'Download uploads/my_part.scad' }).click()
    const saved = await download
    expect(saved.suggestedFilename()).toBe('my_part.scad')

    await files.getByRole('button', { name: 'Delete uploads/my_part.scad' }).click()
    await files.getByRole('group', { name: 'Delete uploads/my_part.scad?' }).getByRole('button', { name: 'Delete file' }).click()
    await expect(files.getByText(/No files yet/)).toBeVisible()
  })
})
