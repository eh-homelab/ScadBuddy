import type { APIRequestContext } from '@playwright/test'
import { randomUUID } from 'node:crypto'

/**
 * One deliberate press, as the app's own client sends it: a fresh `Idempotency-Key`,
 * which every operation route requires since #1777 (a keyless write answers 428).
 */
export function idempotencyKey(): Record<string, string> {
  return { 'Idempotency-Key': randomUUID() }
}

/**
 * A test's cleanup delete: never fails the test (it runs in `finally`, after the real
 * assertion), but says so when the route refused it, so a contract change cannot leave
 * models behind unnoticed.
 */
export async function deleteModel(request: APIRequestContext, slug: string): Promise<void> {
  const response = await request.delete(`/api/v1/models/${slug}`, { headers: idempotencyKey() })
  if (!response.ok()) console.warn(`cleanup: DELETE /api/v1/models/${slug} answered ${response.status()}`)
}
