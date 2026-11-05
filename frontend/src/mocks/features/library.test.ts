import { describe, expect, it } from 'vitest'
import { api, ApiError } from '../../api/client'
import type { PrintRunRequest } from '../../api/types'
import { MULTI_PLATE_FILE } from './library'

describe('library print check mock (#755)', () => {
  it('answers a verdict for a printable file', async () => {
    await expect(api.checkLibraryPrint(MULTI_PLATE_FILE, {} as PrintRunRequest)).resolves.toEqual({
      errors: [],
      warnings: [],
      rack: null,
    })
  })

  it('refuses a file the library does not have, as the run does', async () => {
    const refused = api.checkLibraryPrint(999_999, {} as PrintRunRequest)
    await expect(refused).rejects.toBeInstanceOf(ApiError)
    await expect(refused).rejects.toMatchObject({ status: 404 })
  })
})

describe('library listing mock paging (#2290)', () => {
  it('answers a window after a file id, with the whole count', async () => {
    const all = await api.listLibraryPage({ folderId: 9, all: false }, { limit: 500 })
    const first = await api.listLibraryPage({ folderId: 9, all: false }, { limit: 2 })
    const next = await api.listLibraryPage({ folderId: 9, all: false }, { limit: 2, after: first.files![1]!.id })

    expect(first.files!.map((file) => file.id)).toEqual(all.files!.slice(0, 2).map((file) => file.id))
    expect(next.files!.map((file) => file.id)).toEqual(all.files!.slice(2, 4).map((file) => file.id))
    expect(first.total).toBe(all.files!.length)
  })

  it('refuses to page after a file that is not listed', async () => {
    const refused = api.listLibraryPage({ folderId: 9, all: false }, { limit: 2, after: 999_999 })
    await expect(refused).rejects.toMatchObject({ status: 409 })
  })
})
