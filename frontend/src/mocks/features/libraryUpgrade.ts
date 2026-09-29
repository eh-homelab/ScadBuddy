import { HttpResponse, delay, http } from 'msw'
import type { InstalledLibrary, LibraryCheck, LibraryUser, ModelLibrary } from '../../api/types'
import * as fixtures from '../fixtures'
import { mockModels, problem, refuseBuiltin, replaceMockModel } from '../handlers'

/**
 * #169 — the library upgrade flow's routes: who pins a library, the checkouts on the
 * volume, a candidate ref's parse check, and a re-pin from the URL already pinned
 * (`api/libraries.py`). The models stay in `handlers.ts`; this module reads and writes
 * their pins through its accessors.
 */

const base = '/api/v1'

/** A ref whose checkout the model does not parse against, so a failing check is reachable. */
export const BREAKING_REF = 'v3.0.0'
export const BREAKING_MESSAGE = "Can't open library 'BOSL2/std.scad'."
/** A ref whose check OpenSCAD is killed on the render timeout, as `check_source` reports it. */
export const SLOW_REF = 'v3.1.0'
export const SLOW_MESSAGE = 'the check timed out after 60s'
/** A ref answered as if no openscad binary were on PATH: ok, but not checked. */
export const UNCHECKED_REF = 'v3.2.0'

/** A commit for `ref` that stays the same across calls, as a tag's does upstream. */
function commitOf(ref: string): string {
  let hash = 0
  for (const char of ref) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return hash.toString(16).padStart(8, '0').repeat(5)
}

/** Seeds a pin into a model, built-ins included, as if its model.json said so. */
export function setMockLibraryPin(slug: string, pin: ModelLibrary): void {
  const model = mockModels().find((m) => m.slug === slug)
  if (!model) throw new Error(`no mock model ${slug}`)
  const rest = (model.libraries ?? []).filter((row) => row.name !== pin.name)
  replaceMockModel({ ...model, libraries: [...rest, pin] })
}

function declared(slug: string, name: string): ModelLibrary | undefined {
  return mockModels()
    .find((m) => m.slug === slug)
    ?.libraries?.find((row) => row.name === name)
}

export const handlers = [
  http.get(`${base}/libraries/installed`, () => {
    const installed = new Map<string, InstalledLibrary>()
    for (const model of mockModels()) {
      for (const pin of model.libraries ?? []) {
        const key = `${pin.name}\u0000${pin.commit}`
        const row = installed.get(key) ?? { name: pin.name, commit: pin.commit, used_by: [] }
        row.used_by = [...(row.used_by ?? []), model.slug]
        installed.set(key, row)
      }
    }
    return HttpResponse.json([...installed.values()])
  }),

  http.get(`${base}/libraries/:name/users`, ({ params }) => {
    const name = String(params['name'])
    const users: LibraryUser[] = []
    for (const model of mockModels()) {
      const pin = model.libraries?.find((row) => row.name === name)
      if (pin) {
        users.push({ slug: model.slug, url: pin.url, ref: pin.ref, commit: pin.commit })
      } else if (model.invalid_libraries?.some((entry) => entry.name === name)) {
        users.push({ slug: model.slug, url: null, ref: null, commit: null })
      }
    }
    return HttpResponse.json(users)
  }),

  // A built-in can be checked too: nothing is recorded.
  http.post(`${base}/models/:slug/libraries/:name/check`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const name = String(params['name'])
    const pin = declared(slug, name)
    if (!pin) return problem(404, 'Not Found', `'${slug}' does not declare a library named '${name}'`)
    const body = (await request.json().catch(() => null)) as { ref?: string | null } | null
    const ref = body?.ref ?? fixtures.libraries.find((entry) => entry.name === name)?.ref ?? pin.ref
    if (ref === fixtures.MISSING_REF) {
      return problem(502, 'Bad Gateway', `git clone failed: Remote branch ${ref} not found`)
    }
    await delay(100)
    const broken = ref === BREAKING_REF
    const slow = ref === SLOW_REF
    const candidate = { ref, commit: commitOf(ref) }
    const check: LibraryCheck =
      ref === UNCHECKED_REF
        ? { ok: true, checked: false, timed_out: false, diagnostics: [], log_tail: [], parameters: null, ...candidate }
        : slow
          ? {
              ok: false,
              checked: true,
              timed_out: true,
              diagnostics: [{ severity: 'error', message: SLOW_MESSAGE }],
              log_tail: [],
              parameters: null,
              ...candidate,
            }
          : {
              ok: !broken,
              checked: true,
              timed_out: false,
              diagnostics: broken
                ? [{ severity: 'error', message: BREAKING_MESSAGE, file: 'model.scad', line: 1 }]
                : [],
              log_tail: broken ? [`WARNING: ${BREAKING_MESSAGE}`, 'Execution aborted'] : [],
              parameters: broken ? null : 4,
              ...candidate,
            }
    return HttpResponse.json(check)
  }),

  http.patch(`${base}/models/:slug/libraries/:name`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const name = String(params['name'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const model = mockModels().find((m) => m.slug === slug)
    if (!model) return problem(404, 'Not Found', `no model named '${slug}'`)
    const current = declared(slug, name)
    if (!current) return problem(404, 'Not Found', `'${slug}' does not declare a library named '${name}'`)
    const body = (await request.json().catch(() => null)) as { ref?: string | null } | null
    const ref = body?.ref || current.ref
    if (ref === fixtures.MISSING_REF) {
      return problem(502, 'Bad Gateway', `git clone failed: Remote branch ${ref} not found`)
    }
    await delay(100)
    const pin: ModelLibrary = { name, url: current.url, ref, commit: commitOf(ref) }
    const libraries = (model.libraries ?? []).map((row) => (row.name === name ? pin : row))
    return HttpResponse.json(replaceMockModel({ ...model, libraries }))
  }),
]
