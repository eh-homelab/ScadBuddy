import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { SEND_PENDING_TEXT, sendFailure, writeFailure } from './presetWrites'

function pending() {
  return new ApiError({ title: 'Unavailable', status: 503, detail: 'not done yet', code: 'preset_write_pending' } as never)
}

describe('sendFailure', () => {
  it('reads a pending write as a send, not as a copy or an edit', () => {
    expect(writeFailure(pending()).text).toContain('a copy or an edit')
    const failure = sendFailure(pending())
    expect(failure.text).toBe(SEND_PENDING_TEXT)
    expect(failure.pending).toBe(true)
  })
})
