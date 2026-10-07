import { randomUUID } from 'node:crypto'

/**
 * One deliberate press, as the app's own client sends it: a fresh `Idempotency-Key`,
 * which every operation route requires since #1777 (a keyless write answers 428).
 */
export function press(): Record<string, string> {
  return { 'Idempotency-Key': randomUUID() }
}
