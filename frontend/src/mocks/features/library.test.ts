import { describe, expect, it } from 'vitest'
import { api, ApiError } from '../../api/client'
import type { PrintRunRequest } from '../../api/types'
import { MULTI_PLATE_FILE } from './library'

describe('library print check mock (#755)', () => {
  it('answers a verdict for a printable file', async () => {
    await expect(api.checkLibraryPrint(MULTI_PLATE_FILE, {} as PrintRunRequest)).resolves.toEqual({
      errors: [],
      warnings: [],
    })
  })

  it('refuses a file the library does not have, as the run does', async () => {
    const refused = api.checkLibraryPrint(999_999, {} as PrintRunRequest)
    await expect(refused).rejects.toBeInstanceOf(ApiError)
    await expect(refused).rejects.toMatchObject({ status: 404 })
  })
})
