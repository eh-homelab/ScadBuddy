import { describe, expect, it } from 'vitest'
import { inventoryProblemMessage, refusedCheck, trackingDuplicates } from './problems'

const base = { title: 'Conflict', status: 409 }

describe('refusedCheck', () => {
  const refusal = {
    title: 'Unprocessable Content',
    status: 422,
    diagnostics: [
      { severity: 'error', message: 'Parser error: syntax error', line: 3, file: 'model.scad' },
      { severity: 'warning', message: "Can't find include file 'lib.scad'.", line: 1 },
    ],
    log_tail: ['ERROR: Parser error: syntax error in file model.scad, line 3'],
  }

  it('rebuilds the check that refused the save', () => {
    const check = refusedCheck(refusal)
    expect(check?.ok).toBe(false)
    expect(check?.diagnostics).toHaveLength(2)
    expect(check?.log_tail).toEqual(refusal.log_tail)
  })

  it('carries the timeout flag the server sent', () => {
    expect(refusedCheck(refusal)?.timed_out).toBe(false)
    expect(refusedCheck({ ...refusal, timed_out: true })?.timed_out).toBe(true)
  })

  it('is undefined when the problem carries no diagnostics', () => {
    expect(refusedCheck({ title: 'Conflict', status: 409 })).toBeUndefined()
  })
})

describe('trackingDuplicates', () => {
  it('reads the slugs of a delete refused for duplicates', () => {
    expect(trackingDuplicates({ ...base, duplicates: 2, slugs: ['a', 'b'] })).toEqual(['a', 'b'])
  })

  it('is undefined when no duplicate is named', () => {
    expect(trackingDuplicates({ ...base, duplicates: 0, slugs: [] })).toBeUndefined()
    expect(trackingDuplicates({ ...base, duplicates: 1, slugs: [] })).toBeUndefined()
    expect(trackingDuplicates({ ...base, duplicates: 0, slugs: ['a'] })).toBeUndefined()
  })

  it('is undefined for any other conflict', () => {
    expect(trackingDuplicates({ ...base, detail: 'render in progress' })).toBeUndefined()
  })
})

describe('inventoryProblemMessage (#6949 K1)', () => {
  const of = (status: number, code: string, extra: Record<string, unknown> = {}) => ({ title: 'x', status, detail: 'go detail', code, ...extra })

  it('names every code the Go inventory answers', () => {
    for (const [status, code] of [
      [409, 'step_not_applicable'],
      [409, 'step_incomplete'],
      [409, 'handler_not_built'],
      [409, 'wrong_counting'],
      [409, 'recost_orders_moved'],
      [409, 'kind_in_use'],
      [422, 'class_open'],
      [422, 'class_invalid'],
      [422, 'bad_limit'],
      [422, 'bad_input'],
    ] as const) {
      const message = inventoryProblemMessage(of(status, code))
      expect(message, code).toBeTruthy()
      expect(message, code).not.toBe('go detail')
    }
  })

  it('lists each step a steps_invalid answer names', () => {
    const message = inventoryProblemMessage(
      of(422, 'steps_invalid', { problems: [{ owner: 'part', step: 'take', handler: 'fifo', reason: 'no such handler' }, { step: 'receive', handler: 'lot', reason: 'needs a place', missing: ['location'] }] }),
    )
    expect(message).toContain('take')
    expect(message).toContain('no such handler')
    expect(message).toContain('receive')
    expect(message).toContain('needs a place')
  })

  it('names the kind and the key a class_changed answer changes', () => {
    const message = inventoryProblemMessage(of(422, 'class_changed', { owner: 'hardware', key: 'counting', from: 'lot', to: 'unit' }))
    expect(message).toContain('hardware')
    expect(message).toContain('counting')
    expect(message).toContain('lot')
    expect(message).toContain('unit')
  })

  it('leaves a code it does not know to the caller', () => {
    expect(inventoryProblemMessage(of(409, 'lot_empty'))).toBeUndefined()
  })
})
