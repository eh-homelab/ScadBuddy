import { expect, test } from '@playwright/test'

/**
 * Voice input (#257) with the Web Speech API replaced before the app loads: Chromium's
 * real `webkitSpeechRecognition` sends audio to a server and needs a microphone, so the
 * test drives a fake one through `window.__speech`.
 */
test.describe('voice input (#257)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      type Handler = ((event: unknown) => void) | null
      class FakeRecognition {
        lang = ''
        continuous = false
        interimResults = false
        onresult: Handler = null
        onerror: Handler = null
        onend: (() => void) | null = null
        start() {
          ;(globalThis as unknown as { __speech: FakeRecognition }).__speech = this
        }
        stop() {
          this.onend?.()
        }
        abort() {}
        hear(transcript: string, isFinal: boolean) {
          this.onresult?.({ resultIndex: 0, results: [{ isFinal, 0: { transcript } }] })
        }
      }
      Object.assign(globalThis, { webkitSpeechRecognition: FakeRecognition, SpeechRecognition: FakeRecognition })
    })
  })

  test('dictates into the composer for the user to review; nothing is sent', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    const mic = panel.getByRole('button', { name: 'Voice input' })

    await mic.click()
    await expect(mic).toHaveAttribute('aria-pressed', 'true')
    await page.evaluate(() =>
      (globalThis as unknown as { __speech: { hear: (t: string, f: boolean) => void } }).__speech.hear(
        'make the name Reagan',
        true,
      ),
    )
    await expect(composer).toHaveValue('make the name Reagan')
    await mic.click()
    await expect(mic).toHaveAttribute('aria-pressed', 'false')
    await expect(composer).toBeFocused()
    await expect(composer).toHaveValue('make the name Reagan')
    await expect(panel.getByRole('log', { name: 'Conversation' }).getByText('make the name Reagan')).toHaveCount(0)
  })
})
