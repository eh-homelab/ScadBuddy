import { describe, expect, it } from 'vitest'
import type { AnalysisRequest, AnalyzerDecision, ScopeRef } from '../api/types'
import { analysisReport, analysisScopes, openEdgesDiagnostic, resolveDecision } from './analyzers'
import * as fixtures from './fixtures'

/**
 * The mock resolves decisions as `backend/scadbuddy/analyzers/decisions.py` `resolve`
 * does, at `context.scopes_for(slots)`, or the msw tests pass against behaviour the
 * real server does not have.
 */

const output = fixtures.outputs[0]!
const request: AnalysisRequest = { printer_id: 1, plate_id: 1, all_plates: false }

function decision(scope: ScopeRef, over: Partial<AnalyzerDecision> = {}): AnalyzerDecision {
  return {
    id: `${scope.kind}-${scope.key}`,
    diagnostic_id: openEdgesDiagnostic.id,
    instance: null,
    kind: 'suppress',
    scope,
    reason: 'r',
    enforced: false,
    created_at: '2026-09-28T00:00:00Z',
    ...over,
  }
}

describe('resolveDecision', () => {
  const scopes = analysisScopes(output, request)
  const model = decision({ kind: 'printer', key: 'model:h2c' })
  const printer = decision({ kind: 'printer', key: 'id:1' })

  it('ranks a printer id above the printer model, as the backend ranks by position', () => {
    expect(resolveDecision(openEdgesDiagnostic, [printer, model], scopes)).toBe(printer)
    expect(resolveDecision(openEdgesDiagnostic, [model, printer], scopes)).toBe(printer)
  })

  it('takes the broadest enforced decision', () => {
    const enforcedModel = { ...model, enforced: true }
    const enforcedPrinter = { ...printer, enforced: true }
    expect(
      resolveDecision(openEdgesDiagnostic, [enforcedPrinter, enforcedModel], scopes),
    ).toBe(enforcedModel)
  })
})

describe('analysisReport', () => {
  const petg = decision({ kind: 'material', key: 'petg' })

  it("applies a material decision only to a finding about that material's slot", () => {
    const report = (slots: number[]) =>
      analysisReport(output, request, [{ ...openEdgesDiagnostic, slots }], [petg])
        .diagnostics[0]!.status
    expect(report([1])).toBe('open')
    expect(report([2])).toBe('suppressed')
    expect(report([])).toBe('suppressed')
  })
})
