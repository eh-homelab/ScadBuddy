import { readFileSync } from 'node:fs'
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

  // The codes and statuses of Go's `routes.Codes`, read from its source (the one list of
  // the inventory's codes), so a code Go adds without a message here fails this test.
  const goCodes = (() => {
    const source = readFileSync(`${import.meta.dirname}/../../../tools/go/core/internal/inventory/routes/problems.go`, 'utf8')
    const block = /var Codes = map\[string\]int\{([\s\S]*?)\n\}/.exec(source)![1]!
    return [...block.matchAll(/"([a-z_]+)":\s*(\d+)/g)].map((m) => [m[1]!, Number(m[2])] as const)
  })()

  it("reads Go's code list", () => {
    expect(goCodes.length).toBeGreaterThan(30)
    expect(goCodes).toContainEqual(['handler_not_built', 409])
  })

  it('names every code the Go inventory answers', () => {
    for (const [code, status] of goCodes) {
      const message = inventoryProblemMessage(of(status, code))
      expect(message, code).toBeTruthy()
      expect(message, code).not.toContain('undefined')
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

  it('names the kind and the key a class_changed answer changes, as Go sends it in problems', () => {
    // Go's shape: store/kinds.go classRefusal and SaveKind, `problems: [{owner?, key, from, to}]`.
    const message = inventoryProblemMessage(of(422, 'class_changed', { problems: [{ owner: 'item_kind:hardware', key: 'counting', from: 'lot', to: 'unit' }] }))
    expect(message).toContain('hardware')
    expect(message).toContain('counting')
    expect(message).toContain('lot')
    expect(message).toContain('unit')
    expect(message).not.toContain('undefined')
  })

  it('names the saved kind when a class_changed problem has no owner, and an unset key', () => {
    const message = inventoryProblemMessage(of(422, 'class_changed', { problems: [{ key: 'billing', from: null, to: 'flat' }] }))
    expect(message).toContain('billing')
    expect(message).toContain('flat')
    expect(message).not.toContain('undefined')
    expect(message).not.toContain('null')
  })

  it('leaves a code it does not know to the caller', () => {
    expect(inventoryProblemMessage(of(409, 'recost_busy'))).toBeUndefined()
  })
})
