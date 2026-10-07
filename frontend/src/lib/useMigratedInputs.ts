import { api } from '../api/client'
import { isJsonObject, type JsonObject } from './inputs'

export type MigrateOutcome =
  | { kind: 'ready'; inputs: JsonObject }
  | { kind: 'failed'; inputs: JsonObject; error: string }

/** Saved inputs, brought up to the template's INPUTS_VERSION (spec §8.2). */
export async function migrateIfOld(
  slug: string,
  inputs: JsonObject,
  current: number,
  version?: string,
): Promise<MigrateOutcome> {
  const v = typeof inputs.v === 'number' ? inputs.v : 0
  if (v === current) return { kind: 'ready', inputs }
  try {
    const result = await api.migrateInputs(slug, inputs, version)
    return { kind: 'ready', inputs: isJsonObject(result.inputs) ? (result.inputs as JsonObject) : inputs }
  } catch (error) {
    return { kind: 'failed', inputs, error: error instanceof Error ? error.message : String(error) }
  }
}
