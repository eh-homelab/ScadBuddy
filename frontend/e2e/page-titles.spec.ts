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
  // Orders (#4249), with the mock's fixtures (src/mocks/features/orders/state.ts).
  '/orders',
  '/orders/0e5a3c1e-1111-4222-8333-944455556661',
  '/customers',
  '/customers/c1a00000-0000-4000-8000-000000000001',
  // The component gallery (#3653): in the mocked build only, which this spec runs on.
  '/ui-gallery',
]

for (const path of PAGES) {
  test(`${path} names its tab from its heading`, async ({ page }) => {
    await page.goto(path)
    await expect(page).toHaveTitle(/^\S.* · ScadBuddy$/)
  })
}

test('covers every route App.tsx declares', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  // Only literal path="…" routes are seen here, so a path={…} fails the check (#3394).
  // A tripwire for App.tsx's own form, not a parser: a route spread from props
  // (<Route {...props}>) or declared in a route-config object would still slip past it.
  // The index route is '/', which PAGES lists.
  expect(app, 'declare each route with a literal path="…"').not.toMatch(/\bpath=\{/)
  const routes = [...app.matchAll(/path="([^"]+)"/g)].map((m) => m[1]!).filter((p) => p !== '*')
  const pattern = (route: string) =>
    new RegExp(`^/${route.replace(/:[a-zA-Z]+/g, '[^/]+').replace(/\/\*$/, '(/.*)?')}$`)
  const missing = routes.filter((route) => !PAGES.some((path) => pattern(route).test(path)))
  expect(missing, 'add a page for each new route').toEqual([])
})
