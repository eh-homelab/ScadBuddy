import { describe, expect, it, vi } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import type { JsonObject } from '../lib/inputs'
import { createHost, HostInputError, type HostDeps } from './host'
import { supportedMajors, UI_API_SUPPORTED } from './types'

function deps(overrides: Partial<HostDeps> = {}): HostDeps & { state: { inputs: JsonObject } } {
  const state = { inputs: { params: { name: 'Hi' }, tab: 'a' } as JsonObject }
  return {
    state,
    slug: 'name-keychain',
    version: undefined,
    getSchema: () => keychainSchema,
    getInputs: () => state.inputs,
    setInputs: (next) => {
      state.inputs = next
    },
    generate: vi.fn(async () => ({ jobId: 'j', outputId: 'o' })),
    openPrint: vi.fn(),
    presets: { list: vi.fn(async () => []), save: vi.fn(), load: vi.fn(async () => undefined) },
    onDescribe: vi.fn(),
    ...overrides,
  }
}

describe('createHost', () => {
  it('applies a merge patch to the inputs', () => {
    const d = deps()
    createHost(d).host.inputs.set({ params: { name: 'Yo' }, tab: null, house: { storeys: 2 } })
    expect(d.state.inputs).toEqual({ params: { name: 'Yo' }, house: { storeys: 2 } })
  })

  it('hands out copies, so a UI cannot mutate the page state behind its back', () => {
    const d = deps()
    const got = createHost(d).host.inputs.get()
    ;(got['params'] as JsonObject)['name'] = 'mutated'
    expect(d.state.inputs).toEqual({ params: { name: 'Hi' }, tab: 'a' })
  })

  it('refuses a parameter the schema lacks', () => {
    const d = deps()
    expect(() => createHost(d).host.inputs.set({ params: { nope: 1 } })).toThrow(HostInputError)
    expect(() => createHost(d).host.inputs.set({ params: { nope: 1 } })).toThrow(/nope/)
    expect(d.state.inputs).toEqual({ params: { name: 'Hi' }, tab: 'a' })
  })

  it('refuses removing params or a non-scalar parameter value', () => {
    const d = deps()
    const { host } = createHost(d)
    expect(() => host.inputs.set({ params: null })).toThrow(HostInputError)
    expect(() => host.inputs.set({ params: { name: ['a'] } })).toThrow(/name/)
  })

  it('deletes one parameter override with null, as a merge patch does', () => {
    const d = deps()
    createHost(d).host.inputs.set({ params: { name: null } })
    expect(d.state.inputs).toEqual({ params: {}, tab: 'a' })
    expect(() => createHost(d).host.inputs.set({ params: { nope: null } })).toThrow(/nope/)
  })

  it('notifies subscribers until they unsubscribe', () => {
    const handle = createHost(deps())
    const seen: JsonObject[] = []
    const unsubscribe = handle.host.inputs.subscribe((inputs) => seen.push(inputs))
    handle.notify({ params: { name: 'A' } })
    unsubscribe()
    handle.notify({ params: { name: 'B' } })
    expect(seen).toEqual([{ params: { name: 'A' } }])
  })

  it('ignores writes after dispose', async () => {
    const d = deps()
    const handle = createHost(d)
    const seen = vi.fn()
    handle.host.inputs.subscribe(seen)
    handle.dispose()
    handle.host.inputs.set({ params: { name: 'late' } })
    handle.host.openPrint('o')
    handle.notify({ params: { name: 'x' } })
    await expect(handle.host.generate()).rejects.toThrow(/unmounted/)
    // Reads too: a disposed host never answers with whatever the page shows next.
    expect(() => handle.host.inputs.get()).toThrow(/unmounted/)
    await expect(handle.host.schema()).rejects.toThrow(/unmounted/)
    await expect(handle.host.presets.list()).rejects.toThrow(/unmounted/)
    await expect(handle.host.presets.save('n')).rejects.toThrow(/unmounted/)
    await expect(handle.host.presets.load('p')).rejects.toThrow(/unmounted/)
    expect(d.presets.list).not.toHaveBeenCalled()
    expect(d.presets.save).not.toHaveBeenCalled()
    expect(d.presets.load).not.toHaveBeenCalled()
    const late = vi.fn()
    handle.host.inputs.subscribe(late)
    handle.notify({ params: { name: 'y' } })
    expect(late).not.toHaveBeenCalled()
    expect(d.state.inputs).toEqual({ params: { name: 'Hi' }, tab: 'a' })
    expect(d.openPrint).not.toHaveBeenCalled()
    expect(seen).not.toHaveBeenCalled()
    expect(d.onDescribe).toHaveBeenLastCalledWith(null)
  })

  it('serves only model.scad as a schema in v1', async () => {
    const { host } = createHost(deps())
    await expect(host.schema()).resolves.toBe(keychainSchema)
    await expect(host.schema('parts/roof.scad')).rejects.toThrow(/model.scad/)
  })

  it('builds asset URLs under ui/, pinned when there is a revision', () => {
    expect(createHost(deps()).host.files.url('img/roof.png')).toBe('/api/v1/models/name-keychain/ui/img/roof.png')
    expect(createHost(deps({ version: 'abc1234' })).host.files.url('a.css')).toBe(
      '/api/v1/models/name-keychain/versions/abc1234/ui/a.css',
    )
    expect(() => createHost(deps()).host.files.url('../model.scad')).toThrow()
  })
})

describe('supportedMajors', () => {
  it('is the current host-API major and the one before it', () => {
    expect(supportedMajors(1)).toEqual([1])
    expect(supportedMajors(2)).toEqual([2, 1])
    expect(UI_API_SUPPORTED).toEqual([1])
  })
})
