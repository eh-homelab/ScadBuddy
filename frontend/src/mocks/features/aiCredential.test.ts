import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../handlers'

// #5772 — the rename route's body is strict in the agent (`routes/credentials.ts` `RenameBody`,
// a zod strictObject): a key other than `name` is a 400. The mock refuses it the same way.

const RENAME = '/api/v1/ai/credentials/entries/default/name'

const rename = (body: unknown) =>
  fetch(RENAME, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })

describe('credential rename mock (#5772)', () => {
  beforeEach(() => resetMockState())

  it('takes a body of `name` only', async () => {
    const ok = await rename({ name: 'Work' })
    expect(ok.status).toBe(200)
    expect(((await ok.json()) as { name: string | null }).name).toBe('Work')
  })

  it('refuses a body with another key, or with no `name`, as the agent does', async () => {
    for (const body of [{ name: 'Work', extra: 1 }, {}, { name: 1 }, []]) {
      const res = await rename(body)
      expect(res.status, JSON.stringify(body)).toBe(400)
    }
  })
})
