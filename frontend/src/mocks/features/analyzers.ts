import { HttpResponse, http } from 'msw'
import type {
  AnalysisRun,
  AnalyzerDecision,
  AnalyzerDiagnostic,
  AnalyzerFix,
  DecisionCreate,
  FixApply,
  FixPreview,
  FixRequest,
} from '../../api/types'
import {
  analysisReport,
  analysisScopes,
  fixDigest,
  fixFingerprint,
  openEdgesDiagnostic,
  overhangDiagnostic,
  scopesFor,
} from '../analyzers'
import { mockOutput, nextHexId, problem, shapeRefusal } from '../handlers'
import { emitRealtime } from '../realtime'

/**
 * #284 — `/api/v1/analyzers/…` for vitest and the mocked e2e run. The report itself is
 * shaped in `mocks/analyzers.ts`; this module is the routes and the decisions they keep
 * (`features/`, #508).
 */

const base = '/api/v1/analyzers'

const state = {
  /** The `analyzer_decisions` table: accepts, ignores and suppressions at a scope. */
  decisions: [] as AnalyzerDecision[],
  /** What the analyzers find on every output; a test can swap it. */
  diagnostics: [overhangDiagnostic, openEdgesDiagnostic] as AnalyzerDiagnostic[],
}

export function reset(): void {
  state.decisions = []
  state.diagnostics = [overhangDiagnostic, openEdgesDiagnostic]
}

/** What every mock analyzer run finds, so run, preview and apply agree. */
export function setMockAnalyzerDiagnostics(diagnostics: AnalyzerDiagnostic[]): void {
  state.diagnostics = diagnostics
}

/**
 * What must be verified before `fix` can be applied, each once, as `Fix.blockers` lists
 * them (`backend/scadbuddy/analyzers/model.py`): the preview's `blockers` and the
 * apply's 409 `to_verify` are the same list.
 */
function blockersOf(fix: AnalyzerFix): string[] {
  return [
    ...new Set(fix.changes.flatMap((row) => (!row.verified && row.to_verify ? [row.to_verify] : []))),
  ]
}

/**
 * The diagnostic and fix a preview or apply names, at the scope it asks for (default the
 * narrowest), or the backend's refusal: 404 for a finding or fix not reported, 422 for a
 * scope this print is not in (`_find_fix`, `_fix_scope`).
 */
function findFix(body: FixRequest) {
  const output = mockOutput(body.target.output_id ?? '')
  if (!output) return { refusal: problem(404, 'Output not found') }
  const analysis = body.request ?? { plate_id: 1, all_plates: false }
  const diagnostic = state.diagnostics.find((row) => row.key === body.diagnostic_key)
  if (!diagnostic) {
    return {
      refusal: problem(404, 'Not Found', `${body.diagnostic_key} is not reported for this print (any more)`),
    }
  }
  const fix = diagnostic.fixes?.find((row) => row.id === body.fix_id)
  if (!fix) {
    return { refusal: problem(404, 'Not Found', `${body.diagnostic_key} offers no fix '${body.fix_id}'`) }
  }
  const scopes = scopesFor(analysisScopes(output, analysis), diagnostic.slots ?? [])
  const scope = body.scope ?? scopes[scopes.length - 1]!
  if (!scopes.some((row) => row.kind === scope.kind && row.key === scope.key)) {
    return {
      refusal: problem(
        422,
        'Unprocessable Content',
        `${diagnostic.key} on this print is not in the ${scope.kind} scope '${scope.key}'`,
      ),
    }
  }
  return { diagnostic, fix, scope, output, analysis }
}

/** A decision about the same rule and instance at the same scope is replaced. */
function record(decision: AnalyzerDecision): void {
  const replaced = state.decisions.filter(
    (row) =>
      row.diagnostic_id === decision.diagnostic_id &&
      (row.instance ?? null) === (decision.instance ?? null) &&
      row.scope.kind === decision.scope.kind &&
      row.scope.key === decision.scope.key,
  )
  state.decisions = [...state.decisions.filter((row) => !replaced.includes(row)), decision]
  for (const row of replaced) announceDecision(row, 'removed')
  announceDecision(decision, 'recorded')
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
        state.diagnostics,
        state.decisions,
      ),
    )
  }),

  /** `POST /analyzers/fixes/preview`: the diff and the fingerprint an apply carries back. */
  http.post(`${base}/fixes/preview`, async ({ request }) => {
    const body = (await request.json()) as FixRequest
    const found = findFix(body)
    if ('refusal' in found) return found.refusal
    const { diagnostic, fix, scope, output, analysis } = found
    const blockers = blockersOf(fix)
    return HttpResponse.json({
      diagnostic_id: diagnostic.id,
      diagnostic_key: diagnostic.key,
      fix,
      scope,
      fingerprint: fixFingerprint(output.id, diagnostic.key, fix, scope, analysis),
      outward: fix.changes.some((row) => row.outward),
      applicable: blockers.length === 0,
      blockers,
      summary: fix.changes
        .map((row) => `${row.setting}: ${row.base_known ? JSON.stringify(row.base) : 'unknown'} → ${JSON.stringify(row.proposed)} [${row.target}]`)
        .join('; '),
      route_note:
        'Applying records this diff as a decision at its scope; nothing sends it yet.',
    } satisfies FixPreview)
  }),

  /**
   * `POST /analyzers/fixes/apply`, refusing in the backend's order: a moved fingerprint
   * (409 stale), an unverified target (409 with `to_verify`), no `confirm` (428).
   */
  http.post(`${base}/fixes/apply`, async ({ request }) => {
    const body = (await request.json()) as FixApply
    const found = findFix(body)
    if ('refusal' in found) return found.refusal
    const { diagnostic, fix, scope, output, analysis } = found
    if (fixFingerprint(output.id, diagnostic.key, fix, scope, analysis) !== body.fingerprint) {
      return problem(
        409,
        'Conflict',
        'the diff, its scope, the print or its base differ from the preview; preview it again',
        { type: 'https://scadbuddy.dev/problems/analyzer-fix-stale' },
      )
    }
    const blockers = blockersOf(fix)
    if (blockers.length > 0) {
      return problem(
        409,
        'Conflict',
        'this fix cannot be applied until where its settings land is verified',
        { type: 'https://scadbuddy.dev/problems/analyzer-fix-unverified', to_verify: blockers },
      )
    }
    if (body.confirm !== true) {
      return problem(428, 'Precondition Required', 'confirm the previewed diff to record it', {
        type: 'https://scadbuddy.dev/problems/confirmation-required',
      })
    }
    const decision: AnalyzerDecision = {
      id: nextHexId(),
      diagnostic_id: diagnostic.id,
      instance: diagnostic.key,
      kind: 'accept',
      scope,
      reason: body.reason ?? null,
      enforced: false,
      fix_id: fix.id,
      fingerprint: body.fingerprint,
      diff_digest: fixDigest(fix),
      changes: fix.changes,
      created_at: new Date().toISOString(),
    }
    record(decision)
    return HttpResponse.json(decision)
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
    record(decision)
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
