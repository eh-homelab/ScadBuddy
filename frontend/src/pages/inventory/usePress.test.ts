import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ApiError, UNANSWERED } from '../../api/client'
import type { WriteOptions } from './api'
import { usePress } from './usePress'

/** A write that records its key and fails as `error` says. */
function writer(error?: unknown) {
  const keys: (string | undefined)[] = []
  const write = (options: WriteOptions) => {
    keys.push(options.key)
    return error === undefined ? Promise.resolve('ok') : Promise.reject(error)
  }
  return { keys, write }
}

async function pressTwice(error: unknown, first: unknown = { qty: '1' }, second: unknown = first) {
  const { result } = renderHook(() => usePress())
  const { keys, write } = writer(error)
  for (const body of [first, second]) {
    await act(async () => {
      await result.current.press(write, body).catch(() => undefined)
    })
  }
  return keys
}

describe('usePress (#6949 K1)', () => {
  it('keeps the key after an answer that never came, so a try again cannot write twice', async () => {
    const lost = new ApiError({ type: UNANSWERED, title: 'No answer', status: 504, detail: 'no answer' })
    const [first, second] = await pressTwice(lost)
    expect(first).toBeTruthy()
    expect(second).toBe(first)
  })

  it('takes a new key after a refusal', async () => {
    const refused = new ApiError({ title: 'Conflict', status: 409, detail: 'lot_empty', code: 'lot_empty' })
    const [first, second] = await pressTwice(refused)
    expect(second).not.toBe(first)
  })

  it('takes a new key for another body, even after a lost answer', async () => {
    const lost = new ApiError({ type: UNANSWERED, title: 'No answer', status: 504, detail: 'no answer' })
    const [first, second] = await pressTwice(lost, { qty: '1' }, { qty: '2' })
    expect(second).not.toBe(first)
  })

  it('reads a body the same in any key order', async () => {
    const lost = new ApiError({ type: UNANSWERED, title: 'No answer', status: 504, detail: 'no answer' })
    const [first, second] = await pressTwice(lost, { a: 1, b: { c: 2, d: 3 } }, { b: { d: 3, c: 2 }, a: 1 })
    expect(second).toBe(first)
  })
})
