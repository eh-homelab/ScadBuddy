import { defineConfig, devices } from '@playwright/test'

/**
 * The smoke test runs against the production bundle with the msw worker serving
 * the API, so it exercises the same build the backend will host.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'on-first-retry',
    colorScheme: 'dark',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'pnpm build && pnpm preview --port 4173',
    url: 'http://127.0.0.1:4173',
    env: { VITE_MOCK_API: '1' },
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
})
