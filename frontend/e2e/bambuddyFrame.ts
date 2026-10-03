import type { FrameLocator, Page } from '@playwright/test'

/**
 * ScadBuddy at `path` inside Bambuddy's External Link frame: another origin (the other
 * loopback name) and its sandbox flags, without allow="clipboard-write".
 */
export async function bambuddyFrame(page: Page, baseURL: string | undefined, path: string): Promise<FrameLocator> {
  const host = new URL('/mockServiceWorker.js', baseURL)
  host.hostname = host.hostname === 'localhost' ? '127.0.0.1' : 'localhost'
  // Bambuddy's page, not ours: served without ScadBuddy's page CSP, which would refuse
  // to frame another origin.
  await page.route(host.href, (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html>' }))
  // A routed page is not on the loopback address space, so Chrome's Local Network
  // Access checks refuse its loopback frame. Bambuddy on the LAN is local; grant it.
  // Chromium's permission: another browser's context throws on the unknown name.
  if (page.context().browser()?.browserType().name() === 'chromium') {
    await page.context().grantPermissions(['local-network-access'])
  }
  await page.goto(host.href)
  await page.setContent(
    `<iframe src="${new URL(path, baseURL).href}" title="ScadBuddy"
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
      style="position: fixed; inset: 0; width: 100%; height: 100%; border: 0"></iframe>`,
  )
  return page.frameLocator('iframe')
}
