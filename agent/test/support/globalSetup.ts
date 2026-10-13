import { ensureTemplate } from './postgres.js'

/** Runs once, before any test file: see throwawayDatabase. */
export async function setup(): Promise<void> {
  await ensureTemplate()
}
