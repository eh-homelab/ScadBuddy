import { HttpResponse, http } from 'msw'
import type { AnalysisRun, AnalyzerDecision, DecisionCreate } from '../../api/types'
import { analysisReport } from '../analyzers'
import { mockOutput, nextHexId, problem, shapeRefusal } from '../handlers'
import { emitRealtime } from '../realtime'

/**
 * #284 — `/api/v1/analyzers/…` for vitest and the mocked e2e run. The report itself is
 * shaped in `mocks/analyzers.ts`; this module is the routes and the decisions they keep
 * (`features/`, #508).
 */

const base = '/api/v1/analyzers'

const state = {
  /** The `analyzer_decisions` table: ignores and suppressions at a scope. */
  decisions: [] as AnalyzerDecision[],
}

export function reset(): void {
  state.decisions = []
}

/** The decisions stored so far, for a test that answers `/run` with its own findings. */
export function storedDecisions(): AnalyzerDecision[] {
  return state.decisions
}

/** `analyzer.decision` on the `analyzers` topic, ids only (`core/events.py`). */
function announceDecision(decision: AnalyzerDecision, action: 'recorded' | 'removed'): void {
  emitRealtime('analyzer.decision', ['analyzers'], {
    decision_id: decision.id,
    diagnostic_id: decision.diagnostic_id,
    scope: decision.scope.kind,
    scope_key: decision.scope.key,
    action,
  })
}

export const handlers = [
  /**
   * `POST /analyzers/run` on an output: the keychain's two findings (`mocks/analyzers.ts`).
   * A configuration target (`slug` + `params`) is not something the dialog sends.
   */
  http.post(`${base}/run`, async ({ request }) => {
    const body = (await request.json()) as AnalysisRun
    const output = mockOutput(body.target.output_id ?? '')
    if (!output) return problem(404, 'Output not found')
    return HttpResponse.json(
      analysisReport(
        output,
        body.request ?? { plate_id: 1, all_plates: false },
        undefined,
        state.decisions,
      ),
    )
  }),

  /**
   * Ignore or suppress at a scope (`post_decision`): a suppression without a reason is
   * refused as the backend's validator refuses it, and a decision about the same rule
   * and instance at the same scope is replaced, each announced on `analyzers`.
   */
  http.post(`${base}/decisions`, async ({ request }) => {
    const body = (await request.json()) as DecisionCreate
    if (body.kind === 'suppress' && !body.reason?.trim()) {
      // `DecisionCreate._well_formed` is a model validator, refused while the body is parsed.
      return shapeRefusal('Value error, a suppression needs a reason, as #pragma warning disable does', [
        'body',
      ])
    }
    const instance = body.instance ?? null
    const replaced = state.decisions.filter(
      (row) =>
        row.diagnostic_id === body.diagnostic_id &&
        (row.instance ?? null) === instance &&
        row.scope.kind === body.scope.kind &&
        row.scope.key === body.scope.key,
    )
    const decision: AnalyzerDecision = {
      id: nextHexId(),
      diagnostic_id: body.diagnostic_id,
      instance,
      kind: body.kind,
      scope: body.scope,
      reason: body.reason?.trim() ?? null,
      enforced: body.enforced ?? false,
      created_at: new Date().toISOString(),
    }
    state.decisions = [...state.decisions.filter((row) => !replaced.includes(row)), decision]
    for (const row of replaced) announceDecision(row, 'removed')
    announceDecision(decision, 'recorded')
    return HttpResponse.json(decision, { status: 201 })
  }),

  http.delete(`${base}/decisions/:id`, ({ params }) => {
    const gone = state.decisions.find((row) => row.id === params['id'])
    if (!gone) return problem(404, 'Not Found', `no decision with id '${String(params['id'])}'`)
    state.decisions = state.decisions.filter((row) => row !== gone)
    announceDecision(gone, 'removed')
    return new HttpResponse(null, { status: 204 })
  }),
]
