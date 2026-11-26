import { expect, test } from '@playwright/test'

/**
 * #7330 — the camera dialog's live view against the msw mocks (`src/mocks/features/camera.ts`):
 * the mocked edge refuses the WHEP offer, as a network that blocks WebRTC's UDP leaves the
 * player, so it plays LL-HLS from the mocked playlist; the playlist never gets a segment, so
 * the player says "Camera unavailable" after its 10 s wait instead of spinning.
 */
test.describe('the live camera (#7330)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no camera')

  test('falls back to HLS, shows the feed states, and Reconnect posts once', async ({ page }) => {
    const offers: string[] = []
    const playlists: string[] = []
    const reconnects: string[] = []
    const reads: string[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      const path = url.pathname
      if (path.includes('/hls/')) reads.push(path.split('/hls/')[1] + url.search)
      if (request.method() === 'POST' && path.endsWith('/whep')) offers.push(path)
      if (path.endsWith('/hls/index.m3u8')) playlists.push(path)
      if (request.method() === 'POST' && path.endsWith('/camera/reconnect')) reconnects.push(path)
    })
    await page.goto('/prints')
    await page.getByRole('button', { name: 'Camera of Spare' }).click()
    const dialog = page.getByRole('dialog', { name: 'Spare camera' })
    const player = dialog.getByTestId('live-player')

    await expect(player).toHaveAttribute('data-state', 'hls')
    await expect(player.getByText('HLS', { exact: true })).toBeVisible()
    await expect.poll(() => playlists.length).toBeGreaterThan(0)
    expect(offers).toEqual(['/api/v1/camera/live/00000000-0000-4000-8000-000000000003/whep'])
    expect(playlists[0]).toBe('/api/v1/camera/live/00000000-0000-4000-8000-000000000003/hls/index.m3u8')
    // MediaMTX's 302 to its cookie check is followed, and the media playlist it names
    // relative to the redirected URL, with the session in the query, is read.
    await expect.poll(() => reads).toEqual(
      expect.arrayContaining(['index.m3u8', 'index.m3u8?cookieCheck=1', 'video1_stream.m3u8?session=mock-session']),
    )

    const feeds = dialog.getByRole('list', { name: 'Camera feeds' })
    await expect(feeds.getByTestId('camera-feed-relay')).toContainText('Live feed')
    await expect(feeds.getByTestId('camera-feed-relay')).toContainText('Receiving')
    await expect(feeds.getByTestId('camera-feed-recorder')).toContainText('Recording')

    await dialog.getByRole('button', { name: 'Reconnect' }).click()
    await expect(dialog.getByText('Reconnecting the camera.')).toBeVisible()
    expect(reconnects).toEqual(['/api/v1/printers/00000000-0000-4000-8000-000000000003/camera/reconnect'])
    // The player starts over (WebRTC first again, then HLS) only once the relay reports
    // `pulling` again: the mocked relay reports `stopped` at 200 ms and `pulling` at 1.5 s.
    await expect(feeds.getByTestId('camera-feed-relay')).toContainText('Stopped')
    expect(offers).toHaveLength(1)
    await expect(feeds.getByTestId('camera-feed-relay')).toContainText('Receiving')
    await expect.poll(() => offers.length).toBe(2)

    await expect(player.getByText('Camera unavailable')).toBeVisible({ timeout: 15_000 })
    await expect(dialog.getByRole('button', { name: 'Reconnect' })).toBeVisible()
    expect(reconnects).toHaveLength(1)
  })

  test('closing the dialog stops the HLS reads', async ({ page }) => {
    const playlists: string[] = []
    page.on('request', (request) => {
      if (new URL(request.url()).pathname.includes('/hls/')) playlists.push(request.url())
    })
    await page.goto('/prints')
    await page.getByRole('button', { name: 'Camera of Spare' }).click()
    const dialog = page.getByRole('dialog', { name: 'Spare camera' })
    await expect(dialog.getByTestId('live-player')).toHaveAttribute('data-state', 'hls')
    await expect.poll(() => playlists.length).toBeGreaterThan(0)

    await dialog.getByRole('button', { name: 'Close' }).click()
    await expect(dialog).toBeHidden()
    const read = playlists.length
    // hls.js reloads a live media playlist about once a target duration (1 s here).
    await page.waitForTimeout(3_000)
    expect(playlists).toHaveLength(read)
  })
})
