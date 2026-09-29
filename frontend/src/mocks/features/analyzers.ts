import { HttpResponse, http } from 'msw'
import type { AnalysisRun } from '../../api/types'
import { analysisReport } from '../analyzers'
import { mockOutput, problem } from '../handlers'

/**
 * #284 — `/api/v1/analyzers/…` for vitest and the mocked e2e run. The report itself is
 * shaped in `mocks/analyzers.ts`; this module is the routes (`features/`, #508).
 */

const base = '/api/v1/analyzers'

export const handlers = [
  /**
   * `POST /analyzers/run` on an output: the keychain's two findings (`mocks/analyzers.ts`).
   * A configuration target (`slug` + `params`) is not something the dialog sends.
   */
  http.post(`${base}/run`, async ({ request }) => {
    const body = (await request.json()) as AnalysisRun
    const output = mockOutput(body.target.output_id ?? '')
    if (!output) return problem(404, 'Output not found')
    return HttpResponse.json(analysisReport(output, body.request ?? { plate_id: 1, all_plates: false }))
  }),
]
