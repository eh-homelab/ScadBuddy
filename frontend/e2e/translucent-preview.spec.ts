import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { settled } from './canvas'

// #6349 — spec §5.1: a translucent or clear colour draws see-through in the 3D preview.
// The viewer marks its canvas with each see-through material's opacity (`data-see-through`).
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

// Screenshots for the plan doc: written only when SCADBUDDY_E2E_SHOTS=1.
const SHOTS = resolve(process.cwd(), '..', 'docs', 'superpowers', 'plans', '2026-11-18-translucent-preview')

async function shot(page: Page, name: string) {
  if (!process.env.SCADBUDDY_E2E_SHOTS) return
  mkdirSync(SHOTS, { recursive: true })
  await page.getByTestId('preview').screenshot({ path: resolve(SHOTS, name) })
}

test('the preview draws a translucent colour see-through and a clear one at the floor', async ({ page }) => {
  await page.goto('/m/name-keychain')
  const canvas = page.getByTestId('preview-canvas').locator('canvas')
  await expect(canvas).toBeVisible()
  // Both colours are opaque: nothing is see-through.
  await expect(canvas).toHaveAttribute('data-see-through', '')

  await page.getByRole('tab', { name: 'Colours' }).click()
  const text = page.getByRole('textbox', { name: 'Text', exact: true })
  await text.fill('#E8532F80')
  await text.blur()
  await expect(canvas).toHaveAttribute('data-see-through', '0.50')
  await shot(page, 'translucent.png')

  // Clear: the alpha is 00, the preview's floor keeps the part in view.
  await text.fill('#E8532F00')
  await text.blur()
  await expect(canvas).toHaveAttribute('data-see-through', '0.35')
  await settled(canvas)
  await shot(page, 'clear.png')

  // The light viewer (`prefers-color-scheme: light`): the same clear part on a white ground.
  // The canvas is keyed by the theme's background (Preview.tsx), so the light theme mounts
  // a new one: wait for the dark one to go, or the checks below can read it while it is
  // replaced ("Element is not attached to the DOM", #7577).
  const dark = await canvas.elementHandle()
  await page.emulateMedia({ colorScheme: 'light' })
  await expect.poll(() => dark?.evaluate((element) => element.isConnected)).toBe(false)
  await expect(canvas).toHaveAttribute('data-see-through', '0.35')
  await settled(canvas)
  await shot(page, 'clear-light.png')
  await page.emulateMedia({ colorScheme: 'dark' })

  // Opaque again.
  await text.fill('#E8532F')
  await text.blur()
  await expect(canvas).toHaveAttribute('data-see-through', '')
})
