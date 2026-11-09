import { describe, expect, it } from 'vitest'
import type { ParamValues } from '../../lib/params'
import { inputsToSend } from './printSetups'

// #4673 PR 4, #5797 review — a slice sends its inputs only when they change the item's.
describe('inputsToSend', () => {
  const inputs = { params: { name: 'Reagan' }, v: 1 }
  const defaults = { name: 'Reagan', text_size: 14 }

  it('sends nothing for no edit, the item’s value typed back, or a default put back', () => {
    expect(inputsToSend(inputs, defaults, inputs.params, {})).toBeNull()
    expect(inputsToSend(inputs, defaults, inputs.params, { name: 'Reagan' })).toBeNull()
    expect(inputsToSend(inputs, defaults, inputs.params, { text_size: 14 })).toBeNull()
  })

  it('sends the item’s inputs with the edits over its parameters', () => {
    expect(inputsToSend(inputs, defaults, inputs.params, { text_size: 16 })).toEqual({ params: { name: 'Reagan', text_size: 16 }, v: 1 })
  })

  it('compares a vector by its items', () => {
    const own = { size: [1, 2] } as unknown as ParamValues
    expect(inputsToSend({ params: own }, {}, own, { size: [1, 2] } as unknown as ParamValues)).toBeNull()
    expect(inputsToSend({ params: own }, {}, own, { size: [1, 3] } as unknown as ParamValues)).toEqual({ params: { size: [1, 3] } })
  })
})
