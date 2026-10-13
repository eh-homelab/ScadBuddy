import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { choicesView, queuedResult } from '../mocks/choices'
import { libraryFileOfScope, optionsSubject, sourceApi, sourceKey } from './printSource'

const OUTPUT = { kind: 'output', output: { id: 'a'.repeat(32), slug: 'name-keychain' } } as const
const LIBRARY = { kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } } as const

describe('printSource', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keys an output and a library file apart', () => {
    expect(sourceKey(OUTPUT)).toBe(`output:${'a'.repeat(32)}`)
    expect(sourceKey(LIBRARY)).toBe('library:89')
    expect(sourceKey(undefined)).toBeUndefined()
  })

  it("keeps an output's options under its model and a library file's under its own key (#1754)", () => {
    expect(optionsSubject(OUTPUT)).toEqual({ key: 'name-keychain', noun: 'model' })
    expect(optionsSubject(LIBRARY)).toEqual({ key: 'library:89', noun: 'file' })
    expect(optionsSubject(undefined)).toBeUndefined()
    expect(libraryFileOfScope('library:89')).toBe(89)
    expect(libraryFileOfScope('builtin:name-keychain')).toBeNull()
  })

  it('routes an output to the output calls', async () => {
    const choices = vi.spyOn(api, 'getChoices').mockResolvedValue(choicesView)
    const remember = vi.spyOn(api, 'putModelChoices').mockResolvedValue({ filament_plan: [] })
    await sourceApi(OUTPUT).getChoices(2)
    await sourceApi(OUTPUT).remember({ filament_plan: [] })
    expect(choices).toHaveBeenCalledWith('a'.repeat(32), 2)
    expect(remember).toHaveBeenCalledWith('name-keychain', { filament_plan: [] })
  })

  it('routes a library file to the library calls', async () => {
    const run = vi.spyOn(api, 'runLibraryPrint').mockResolvedValue(queuedResult)
    const remember = vi.spyOn(api, 'putLibraryChoices').mockResolvedValue({ filament_plan: [] })
    await sourceApi(LIBRARY).run({} as never)
    await sourceApi(LIBRARY).remember({ filament_plan: [] })
    expect(run).toHaveBeenCalledWith(89, {}, undefined, undefined)
    const check = vi.spyOn(api, 'checkLibraryPrint').mockResolvedValue({ errors: [], warnings: [] })
    await sourceApi(LIBRARY).check({} as never)
    expect(check).toHaveBeenCalledWith(89, {})
    expect(remember).toHaveBeenCalledWith(89, { filament_plan: [] })
    expect(sourceApi(LIBRARY).plateThumbnailUrl(2)).toBe('/api/v1/print/library/89/plates/2/thumbnail')
  })
})
