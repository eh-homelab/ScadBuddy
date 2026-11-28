import { expect, test, type APIRequestContext } from '@playwright/test'
import { idempotencyKey } from './operations'

/**
 * #6949 K1 (plan 2026-11-25 Task 15): the inventory goes through Go on a real stack
 * (`e2e-stack/compose.yaml`). The API starts each write as a Temporal standalone activity
 * (`inventory.<Name>`) on the queue `inventory`. scadbuddy-core's worker runs it, writes
 * the stock, and records it in `inventory_operations` with the event
 * `inventory_operation.changed`, which the API puts on the Activities tab. The API reads
 * the inventory from core's CRUD port (`SCADBUDDY_CORE_CRUD_URL`). Without core, each
 * inventory route answers 503 `inventory_unavailable`.
 *
 * `e2e-stack/run.sh` runs this file once, on its first stack, with:
 *
 *   E2E_BASE_URL          the app (http://127.0.0.1:18080)
 *   E2E_INVENTORY_CORE=1  this file's switch: without it (and E2E_BASE_URL) it skips
 */

/** A write's answer: Go's own, or (after 15 s) the operation's result once it ends. */
async function write(request: APIRequestContext, path: string, data: unknown): Promise<Record<string, unknown>> {
  const response = await request.post(`/api/v1${path}`, { data, headers: idempotencyKey() })
  const body = (await response.json()) as Record<string, unknown>
  expect([200, 201, 202], `POST ${path}: ${JSON.stringify(body)}`).toContain(response.status())
  if (response.status() !== 202) return body
  // A slow write: follow its operation to the end.
  const id = encodeURIComponent(String(body.id))
  let operation: Record<string, unknown> = body
  await expect
    .poll(
      async () => {
        operation = (await (await request.get(`/api/v1/inventory/operations/${id}`)).json()) as Record<string, unknown>
        return operation.status
      },
      { message: `the operation of POST ${path}`, timeout: 120_000 },
    )
    .not.toBe('running')
  expect(operation.status, JSON.stringify(operation)).toBe('succeeded')
  return operation.result as Record<string, unknown>
}

test.use({ trace: 'retain-on-failure' })

test.describe('inventory through scadbuddy-core (real stack)', () => {
  test.skip(
    !process.env.E2E_BASE_URL || process.env.E2E_INVENTORY_CORE !== '1',
    'set E2E_BASE_URL and E2E_INVENTORY_CORE=1: e2e-stack/run.sh does',
  )
  test.describe.configure({ retries: 0 })

  test('inventory goes through Go', async ({ page, request }) => {
    test.setTimeout(300_000)

    // Core has no healthcheck in the stack: wait until the API reads through it.
    await expect
      .poll(async () => (await request.get('/api/v1/inventory/kinds')).status(), {
        message: 'GET /api/v1/inventory/kinds through core',
        timeout: 60_000,
      })
      .toBe(200)

    // A kind with no id gets `k` and its number. Under `hardware` its class is closed and
    // it requires no property, so an item of it needs nothing more.
    const kind = await write(request, '/inventory/kinds', { name: 'E2E core inserts', parent_id: 'hardware' })
    expect(String(kind.id)).toMatch(/^k\d+$/)
    const item = await write(request, '/inventory/items', { kind_id: kind.id, name: 'E2E brass insert M3' })
    const itemId = String(item.id)
    expect(item.tracking).toBe('lot')

    await write(request, `/inventory/items/${itemId}/lots`, { qty: '5', unit_cost_minor: '12' })
    await write(request, `/inventory/items/${itemId}/use`, { qty: '1', reason: 'e2e' })

    // Read back through core's CRUD port: one less than received.
    const read = await request.get(`/api/v1/inventory/items/${itemId}`)
    expect(read.status()).toBe(200)
    expect(Number(((await read.json()) as { on_hand: string }).on_hand)).toBe(4)

    // The Activities tab: one row for each of Go's activities on this item, which the API
    // took from `inventory_operation.changed`. The title is the activity's label and the
    // subject (`inventory/activity.py` `title`).
    const subject = `item:${itemId}`
    const rows = [
      { kind: 'inventory.ReceiveLot', title: `Receive a lot ${subject}` },
      { kind: 'inventory.Take', title: `Take stock ${subject}` },
    ]
    await page.goto(`/activity?subject=${encodeURIComponent(subject)}`)
    for (const row of rows) {
      const line = page.getByTestId(/^activity-row-inventory_operation:/).filter({ hasText: row.title })
      await expect(async () => {
        await page.reload()
        await expect(line).toHaveCount(1, { timeout: 5_000 })
        await expect(line.getByText('Succeeded', { exact: true })).toBeVisible({ timeout: 1_000 })
      }, `the ${row.kind} row, succeeded`).toPass({ timeout: 60_000 })
    }

    // The same rows by the API, by kind: Go's activity names, not a Python route's.
    const listed = (await (
      await request.get(
        `/api/v1/activity?${new URLSearchParams([['subject', subject], ...rows.map((row) => ['kind', row.kind])])}`,
      )
    ).json()) as { items: { kind: string; status: string }[] }
    for (const row of rows) {
      expect(listed.items.filter((one) => one.kind === row.kind).map((one) => one.status)).toEqual(['succeeded'])
    }
  })
})
