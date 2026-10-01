import { api } from '../api/client'
import type { CustomizerSchema } from '../api/types'
import { isJsonObject, mergePatch, type JsonObject } from '../lib/inputs'
import { allParams } from '../lib/params'
import { UI_API_CURRENT, type Host } from './types'

/** A write a template UI made that the page refuses; the message names the key. */
export class HostInputError extends Error {
  override name = 'HostInputError'
}

export interface HostDeps {
  slug: string
  version: string | undefined
  getSchema(): CustomizerSchema
  getInputs(): JsonObject
  setInputs(next: JsonObject): void
  generate(): Promise<{ jobId: string; outputId: string }>
  openPrint(outputId: string): void
  presets: Host['presets']
  onDescribe(fn: (() => string) | null): void
}

export interface HostHandle {
  host: Host
  /** Tell the UI the inputs changed (a preset, a reopen, an agent, a widget). */
  notify(inputs: JsonObject): void
  /** The UI is unmounted: everything it still holds becomes a no-op. */
  dispose(): void
}

const UI_PATH = /^(?:[A-Za-z0-9_-][A-Za-z0-9._-]*\/)*[A-Za-z0-9_-][A-Za-z0-9._-]*$/

export function checkedUiPath(path: string): string {
  if (!UI_PATH.test(path)) throw new Error(`not a file under ui/: ${JSON.stringify(path)}`)
  return path
}

function checkedParams(schema: CustomizerSchema, patch: JsonObject): void {
  if (!('params' in patch)) return
  const params = patch['params']
  if (!isJsonObject(params)) throw new HostInputError('inputs.params cannot be removed or replaced by a non-object')
  const names = new Set(allParams(schema).map((param) => param.name))
  for (const [name, value] of Object.entries(params)) {
    if (!names.has(name)) throw new HostInputError(`inputs.params.${name}: model.scad has no parameter "${name}"`)
    if (!['string', 'number', 'boolean'].includes(typeof value)) {
      throw new HostInputError(`inputs.params.${name} must be a number, string or boolean`)
    }
  }
}

function unmounted(): Error {
  return new Error('the template UI is unmounted')
}

export function createHost(deps: HostDeps): HostHandle {
  const listeners = new Set<(inputs: JsonObject) => void>()
  let live = true
  /** Every call checks it: a disposed host never reads or acts on the page again. */
  function alive(): void {
    if (!live) throw unmounted()
  }
  const host: Host = {
    api: UI_API_CURRENT,
    inputs: {
      get: () => {
        alive()
        return structuredClone(deps.getInputs())
      },
      set: (patch) => {
        if (!live) {
          console.warn('ScadBuddy: a template UI wrote its inputs after it was unmounted; ignored')
          return
        }
        checkedParams(deps.getSchema(), patch)
        const next = mergePatch(deps.getInputs(), structuredClone(patch))
        if (!isJsonObject(next)) throw new HostInputError('inputs must stay a JSON object')
        deps.setInputs(next)
      },
      subscribe: (fn) => {
        if (!live) return () => undefined
        listeners.add(fn)
        return () => {
          listeners.delete(fn)
        }
      },
    },
    schema: async (file = 'model.scad') => {
      alive()
      if (file !== 'model.scad') {
        throw new Error(`only model.scad has a customizer schema in host API v1, not ${file}`)
      }
      return deps.getSchema()
    },
    files: { url: (path) => api.uiFileUrl(deps.slug, deps.version, checkedUiPath(path)) },
    generate: () => (live ? deps.generate() : Promise.reject(unmounted())),
    openPrint: (outputId) => {
      if (live) deps.openPrint(outputId)
    },
    presets: {
      list: () => (live ? deps.presets.list() : Promise.reject(unmounted())),
      save: (name) => (live ? deps.presets.save(name) : Promise.reject(unmounted())),
      load: (id) => (live ? deps.presets.load(id) : Promise.reject(unmounted())),
    },
    describe: (fn) => {
      if (live) deps.onDescribe(fn)
    },
  }
  return {
    host,
    notify: (inputs) => {
      if (!live) return
      for (const fn of listeners) fn(structuredClone(inputs))
    },
    dispose: () => {
      live = false
      listeners.clear()
      deps.onDescribe(null)
    },
  }
}
