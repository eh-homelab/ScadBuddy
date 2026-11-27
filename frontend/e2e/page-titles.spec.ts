import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'
import { L, spoolId, T } from '../src/mocks/features/filament/seed'
import { INVOICE, SUPPLIER } from '../src/mocks/features/filamentPurchasingSeed'
import { INVENTORY } from '../src/mocks/features/inventorySeed'

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
  // The old parts inventory (#5543) opens the Inventory on the Part kind (#7067).
  '/orders/parts',
  '/orders/0e5a3c1e-1111-4222-8333-944455556661',
  '/customers',
  '/customers/c1a00000-0000-4000-8000-000000000001',
  // The storefront's listings (#4962): its tab is "Storefront · ScadBuddy".
  '/storefront',
  // A listing's editor (#6157): its tab is the listing's title, "New listing" for a new one.
  '/storefront/listings/new',
  // The Filament section (#6262): each tab, a seeded spool (#3) and a seeded type, from
  // src/mocks/features/filament/seed.ts.
  // `/filament` redirects to the Spools tab.
  '/filament',
  '/filament/spools',
  `/filament/spools/${spoolId(3)}`,
  '/filament/types',
  `/filament/types/${T.JADE}`,
  '/filament/tags',
  // Purchasing (#6266): a seeded supplier and a seeded invoice, from
  // src/mocks/features/filamentPurchasingSeed.ts.
  '/filament/suppliers',
  `/filament/suppliers/${SUPPLIER.BAMBU}`,
  '/filament/invoices',
  `/filament/invoices/${INVOICE.CONFIRMED_A}`,
  '/filament/locations',
  // A place's page and a scanned code (#6955): the code opens the place, slot 1 of AMS-A.
  `/filament/locations/${L.AMS_A1}`,
  '/x/L-4',
  '/filament/catalog',
  '/filament/metrics',
  '/filament/shopping-list',
  '/filament/ledger',
  // Spool labels (#6264): with no spools chosen; the route check below reads paths, not queries.
  '/filament/labels',
  // The phone page that reads tags (#7273, the hand-held Scan mode): without Web NFC it shows
  // how to open it on a phone. `/filament/scan`, its old address, redirects to it.
  '/scan',
  '/filament/scan',
  // Paired phones (#7905), with the mock's two devices (src/mocks/features/devices.ts).
  '/devices',
  // The Inventory area (#7067), with the mock's seeded items (src/mocks/features/inventorySeed.ts).
  '/inventory',
  '/inventory/kinds',
  `/inventory/items/${INVENTORY.HOOK}`,
  `/inventory/units/${INVENTORY.NOZZLE_UNIT}`,
  // A lot opens its item's page, which names the tab.
  `/inventory/lots/${INVENTORY.HOOK_LOT}`,
  // The component gallery (#3653): in the mocked build only, which this spec runs on.
  '/ui-gallery',
]

/**
 * #6399 — the tab title leads with what waits on the person, "(2) Parts · ScadBuddy": the mocks
 * hold an agent's requested run grant and a requested standing grant.
 */
const waiting = (title: string) => new RegExp(`^(\\(\\d+\\) )?${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`)

for (const path of PAGES) {
  test(`${path} names its tab from its heading`, async ({ page }) => {
    await page.goto(path)
    await expect(page).toHaveTitle(/^\S.* · ScadBuddy$/)
  })
}

test('/orders/profit is the profit report, not an order', async ({ page }) => {
  await page.goto('/orders/profit')
  await expect(page).toHaveTitle(waiting('Profit by model · ScadBuddy'))
})

test('/prints/queue/260 is the queue item, not a print', async ({ page }) => {
  await page.goto('/prints/queue/260')
  await expect(page).toHaveTitle(waiting('Bracket.gcode.3mf · ScadBuddy'))
})

test('/orders/parts opens the Inventory on the Part kind, not an order (#7067)', async ({ page }) => {
  await page.goto('/orders/parts')
  await expect(page).toHaveURL(/\/inventory\?kind=part$/)
  await expect(page).toHaveTitle(waiting('Inventory · ScadBuddy'))
})

test('/filament/scan opens the Scan mode at /scan (#7273)', async ({ page }) => {
  await page.goto('/filament/scan')
  await expect(page).toHaveURL(/\/scan$/)
  await expect(page).toHaveTitle(waiting('Scan · ScadBuddy'))
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
