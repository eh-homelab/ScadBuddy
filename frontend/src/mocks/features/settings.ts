import { HttpResponse, http } from 'msw'
import type { BambuddyStatus } from '../../api/types'
import { bambuddyStatus } from '../fixtures'
import { forgetMockRemembered, mockBambuddyUrl, mockRemembered } from '../handlers'

/**
 * #322 — the Settings page's own routes beside `GET`/`PUT /settings` (which stay in
 * `handlers.ts` with the rest of the settings state): what the print dialog remembers,
 * and Bambuddy's read-only status. The remembered choices are the print routes' state,
 * so they are read and forgotten through `handlers.ts`.
 */

const base = '/api/v1'

const state = {
  bambuddyStatus: structuredClone(bambuddyStatus) as BambuddyStatus,
}

export function reset(): void {
  state.bambuddyStatus = structuredClone(bambuddyStatus)
}

export const handlers = [
  // Each entry is forgotten through its own route; this lists them and forgets them all.
  http.get(`${base}/settings/remembered`, () => HttpResponse.json(mockRemembered())),

  http.delete(`${base}/settings/remembered`, () => {
    forgetMockRemembered()
    return HttpResponse.json(mockRemembered())
  }),

  http.get(`${base}/settings/bambuddy`, () => {
    if (!mockBambuddyUrl()) {
      return HttpResponse.json(
        { type: 'about:blank', title: 'Conflict', status: 409, detail: 'no Bambuddy URL is configured' },
        { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
      )
    }
    return HttpResponse.json(state.bambuddyStatus)
  }),
]
