import { describe, expect, it } from 'vitest'
import { acceptOperation } from './operations'

describe('operations mock (#3457)', () => {
  it("answers any feature's recorded operation, not only the preview slice's", async () => {
    const answer = acceptOperation('sweep', 'library', { swept: 3 })
    expect(answer.status).toBe(202)
    const { id } = (await answer.json()) as { id: string }
    const read = await fetch(new URL(`/api/v1/operations/${id}`, window.location.href))
    expect(read.status).toBe(200)
    expect(await read.json()).toMatchObject({ id, kind: 'sweep', status: 'succeeded', result: { swept: 3 } })
  })
})
