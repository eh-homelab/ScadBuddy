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
  // One queue item (#3744), from src/mocks/features/printQueue.ts: "Bracket.gcode.3mf · ScadBuddy".
  '/prints/queue/260',
  '/edit/no-such-output',
  '/library',
  '/workflows',
  '/workflows/flows',
  '/workflows/flows/def-swap',
  '/workflows/runs/run-done',
  '/settings',
  '/settings/tools',
  '/activity',
  // Orders (#4249), with the mock's fixtures (src/mocks/features/orders/state.ts).
  '/orders',
  // The profit report (#5329): its tab is "Profit by model · ScadBuddy".
  '/orders/profit',
  // The parts inventory (#5543): its tab is "Parts · ScadBuddy".
  '/orders/parts',
  '/orders/0e5a3c1e-1111-4222-8333-944455556661',
  '/customers',
  '/customers/c1a00000-0000-4000-8000-000000000001',
  // The storefront's listings (#4962): its tab is "Storefront · ScadBuddy".
  '/storefront',
  // A listing's editor (#6157): its tab is the listing's title, "New listing" for a new one.
  '/storefront/listings/new',
  // The component gallery (#3653): in the mocked build only, which this spec runs on.
  '/ui-gallery',
]

for (const path of PAGES) {
  test(`${path} names its tab from its heading`, async ({ page }) => {
    await page.goto(path)
    await expect(page).toHaveTitle(/^\S.* · ScadBuddy$/)
  })
}

test('/orders/profit is the profit report, not an order', async ({ page }) => {
  await page.goto('/orders/profit')
  await expect(page).toHaveTitle('Profit by model · ScadBuddy')
})

test('/prints/queue/260 is the queue item, not a print', async ({ page }) => {
  await page.goto('/prints/queue/260')
  await expect(page).toHaveTitle('Bracket.gcode.3mf · ScadBuddy')
})

test('/orders/parts is the parts inventory, not an order', async ({ page }) => {
  await page.goto('/orders/parts')
  await expect(page).toHaveTitle('Parts · ScadBuddy')
})

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
