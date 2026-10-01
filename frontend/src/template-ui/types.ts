import type { CustomizerSchema, ParamPreset } from '../api/types'
import type { JsonObject } from '../lib/inputs'

export const UI_API_CURRENT = 1
/** The majors a host at `current` mounts: that one and the one before it (spec §8.1). */
export function supportedMajors(current: number): number[] {
  return [current, current - 1].filter((n) => n > 0)
}
export const UI_API_SUPPORTED: readonly number[] = supportedMajors(UI_API_CURRENT)

export type UiSlot = 'panel' | 'page'

/** `model.json`'s `ui` (spec §4.1), as `ModelRecord.ui` carries it. */
export interface UiDeclaration {
  module: string
  slot?: UiSlot
  api: number
}

export interface MountContext {
  slot: UiSlot
  /** The revision the module was loaded from, or null for the live template. */
  version: string | null
  theme: 'light' | 'dark'
  /** The host-API major this host speaks. */
  api: number
}

/** Host API v1 (spec 2026-09-27 §4.3). */
export interface Host {
  readonly api: number
  inputs: {
    get(): JsonObject
    /**
     * An RFC 7386 merge patch over the inputs; `null` deletes a key, including one
     * parameter override under `params` (the parameter goes back to its default).
     * The host checks only that each parameter is declared and that its value is a
     * number, string or boolean. Ranges, options and formats are the render's job:
     * it refuses an invalid value with a 422 naming the parameter, as the generated
     * form's widgets already rely on.
     */
    set(patch: JsonObject): void
    subscribe(fn: (inputs: JsonObject) => void): () => void
  }
  schema(file?: string): Promise<CustomizerSchema>
  files: { url(path: string): string }
  /** Waits for the render of the current inputs, then keeps it as an output. */
  generate(): Promise<{ jobId: string; outputId: string }>
  openPrint(outputId: string): void
  presets: {
    list(): Promise<ParamPreset[]>
    save(name: string): Promise<ParamPreset>
    load(id: string): Promise<void>
  }
  describe(fn: () => string): void
}

export type MountResult = (() => void) | void
export type Mount = (root: ShadowRoot, host: Host, ctx: MountContext) => MountResult | Promise<MountResult>

export interface TemplateUiFailure {
  /** The template file at fault: the module, or model.json. */
  file: string
  message: string
}
