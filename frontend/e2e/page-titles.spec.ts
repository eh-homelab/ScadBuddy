import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'

// #2454 review — every routed page names its tab from its <h1> (usePageTitle). A page
// with no <h1> inside <main> would quietly get the bare "ScadBuddy" title: one per route
// of App.tsx, with the fixtures the other specs use.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

const PAGES = [
  '/',
  '/new',
  '/m/name-keychain',
  '/m/name-keychain/source',
  '/m/name-keychain/source/helper.scad',
  '/m/name-keychain/history',
  '/m/name-keychain/versions',
  '/m/name-keychain/prints',
  '/prints',
  '/prints/35',
  '/edit/no-such-output',
  '/library',
  '/workflows',
  '/workflows/flows',
  '/workflows/flows/def-swap',
  '/workflows/runs/run-done',
  '/settings',
  '/activity',
]

for (const path of PAGES) {
  test(`${path} names its tab from its heading`, async ({ page }) => {
    await page.goto(path)
    await expect(page).toHaveTitle(/^\S.* · ScadBuddy$/)
  })
}

test('covers every route App.tsx declares', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const routes = [...app.matchAll(/path="([^"]+)"/g)].map((m) => m[1]!).filter((p) => p !== '*')
  const pattern = (route: string) =>
    new RegExp(`^/${route.replace(/:[a-zA-Z]+/g, '[^/]+').replace(/\/\*$/, '(/.*)?')}$`)
  const missing = routes.filter((route) => !PAGES.some((path) => pattern(route).test(path)))
  expect(missing, 'add a page for each new route').toEqual([])
})
