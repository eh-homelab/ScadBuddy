import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { ORDERS_PROBLEMS, refusalText, SUPERSEDED_TEXT } from './refusal'

describe('refusalText', () => {
  it("is the backend's detail", () => {
    const error = new ApiError({ type: `${ORDERS_PROBLEMS}item_locked`, title: 'Conflict', status: 409, detail: 'item i is locked' })
    expect(refusalText(error)).toBe('item i is locked')
  })

  // #4673 PR 3: a slice made for a design that changed since says what to do on the page.
  it('names a slice whose revision was superseded', () => {
    const error = new ApiError({
      type: `${ORDERS_PROBLEMS}revision_superseded`,
      title: 'Conflict',
      status: 409,
      detail: 'item i has a newer revision than the one this slice was made for: slice it again',
    })
    expect(refusalText(error)).toBe(SUPERSEDED_TEXT)
  })
})
