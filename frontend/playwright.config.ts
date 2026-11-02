import { defineConfig, devices } from '@playwright/test'

/**
 * Two modes.
 *
 * Default: the production bundle with the msw worker serving the API, so the smoke
 * test exercises the same build the backend will host.
 *
 * With `E2E_BASE_URL` set: that URL is used as-is and no server is started — this is
 * the real stack (a real OpenSCAD behind the real FastAPI app), which
 * `e2e/real-backend.spec.ts` covers and the msw specs skip.
 */
const realBackend = process.env.E2E_BASE_URL
// Overridable because several worktrees of this repo get checked out side by side, and
// `reuseExistingServer` will happily hand the run another worktree's preview server.
const previewPort = process.env.E2E_PREVIEW_PORT ?? '4173'
// #4975 phase 7: e2e-stack/auth.sh runs e2e/real-auth.spec.ts in all three engines, inside
// the Playwright image on the stack's network. Its https edge is signed by a CA made for the
// run, which no browser trusts: the certificate error is the only thing ignored.
const authEngines = process.env.E2E_AUTH
  ? (
      [
        ['auth-chromium', devices['Desktop Chrome']],
        ['auth-firefox', devices['Desktop Firefox']],
        ['auth-webkit', devices['Desktop Safari']],
      ] as const
    ).map(([name, device]) => ({ name, testMatch: /real-auth\.spec\.ts/, use: { ...device, ignoreHTTPSErrors: true } }))
  : []

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  use: {
    baseURL: realBackend ?? `http://127.0.0.1:${previewPort}`,
    trace: 'on-first-retry',
    colorScheme: 'dark',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }, ...authEngines],
  ...(realBackend
    ? {}
    : {
        webServer: {
          command: `pnpm build && pnpm preview --port ${previewPort}`,
          url: `http://127.0.0.1:${previewPort}`,
          env: { VITE_MOCK_API: '1' },
          reuseExistingServer: !process.env.CI,
          timeout: 180_000,
        },
      }),
})
