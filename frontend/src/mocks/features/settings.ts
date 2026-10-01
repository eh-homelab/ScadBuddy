import { HttpResponse, delay, http } from 'msw'
import type { BambuddyStatus, Settings } from '../../api/types'
import * as fixtures from '../fixtures'
import { forgetMockRemembered, mockRemembered, mockSettings, problem, setMockSettings } from '../handlers'

/**
 * #322 — the Settings page's routes: `GET`/`PUT /settings`, the connection test, what
 * the print dialog remembers, and Bambuddy's read-only status. The settings values and
 * the remembered choices stay in `handlers.ts`, because the send, plate, upload and
 * print routes read them too; this module reads and writes them through its accessors.
 */

const base = '/api/v1'

const state = {
  bambuddyStatus: structuredClone(fixtures.bambuddyStatus) as BambuddyStatus,
  /** The values the mock process "started" with, for `restart_required`. */
  running: structuredClone(fixtures.settings) as Settings,
}

export function reset(): void {
  state.bambuddyStatus = structuredClone(fixtures.bambuddyStatus)
  state.running = structuredClone(fixtures.settings)
}

/** The settings the mock process runs with, as if it had just restarted. */
export function restartMockBackend(): void {
  state.running = structuredClone(mockSettings())
  setMockSettings({ ...mockSettings(), restart_required: [] })
}

const SECRETS = {
  bambuddy_api_key: 'has_api_key',
  bambuddy_render_api_key: 'has_render_api_key',
  google_fonts_api_key: 'has_google_fonts_api_key',
} as const
/** The env-seeded fields a clear can hold; the rest are numbers, switches or a level. */
const NULLABLE = new Set([
  'bambuddy_url',
  'bambuddy_api_key',
  'bambuddy_render_api_key',
  'public_url',
  'default_plate',
  'google_fonts_api_key',
  'temporal_ui_url',
])
const AT_LEAST_ONE = new Set(['render_concurrency', 'check_concurrency', 'library_max_bytes'])
const MORE_THAN_ZERO = new Set(['render_timeout', 'job_ttl', 'media_upload_max_bytes'])

function envName(name: string): string {
  return `SCADBUDDY_${name.toUpperCase()}`
}

function refused(name: string, msg: string) {
  return HttpResponse.json(
    {
      type: 'about:blank',
      title: 'Unprocessable Content',
      status: 422,
      detail: 'the request did not match the expected shape',
      errors: [{ loc: ['body', name], msg }],
    },
    { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

/** Mirrors `SettingsStore.save` and `SettingsPatch`'s checks, then the view's bookkeeping. */
function putSettings(body: Record<string, unknown>) {
  const sources = { ...mockSettings().sources }
  const next: Record<string, unknown> = { ...mockSettings() }
  const reset = (body.reset as string[] | undefined) ?? []
  const envSeeded = new Set(Object.keys(fixtures.settingsApplies))
  for (const name of reset) {
    if (!envSeeded.has(name)) return refused('reset', `${name}: not an env-seeded setting, so nothing to reset`)
  }
  for (const [name, value] of Object.entries(body)) {
    if (name === 'reset') continue
    if (!envSeeded.has(name)) {
      next[name] = name === 'display_unit' ? (value ?? 'mm') : value
      continue
    }
    if (value === null && !NULLABLE.has(name)) {
      return refused(name, `Value error, ${envName(name)} cannot be cleared; reset it to follow the deployment's value`)
    }
    if (name === 'temporal_ui_url' && typeof value === 'string' && !/^https?:\/\//.test(value)) {
      return refused(name, `Value error, ${envName(name)} must be an http(s) URL, not '${value}'`)
    }
    if (typeof value === 'number') {
      if (AT_LEAST_ONE.has(name) && value < 1) return refused(name, `Value error, ${envName(name)} must be at least 1, not ${value}`)
      if (MORE_THAN_ZERO.has(name) && value <= 0) return refused(name, `Value error, ${envName(name)} must be more than 0, not ${value}`)
      if (value < 0) return refused(name, `Value error, ${envName(name)} must be at least 0, not ${value}`)
    }
    if (name in SECRETS) {
      const has = SECRETS[name as keyof typeof SECRETS]
      next[has] = typeof value === 'string' && value.length > 0
      sources[name] = next[has] ? 'stored' : 'cleared'
      continue
    }
    next[name] = name === 'log_level' && typeof value === 'string' ? value.toUpperCase() : value
    sources[name] = value === null ? 'cleared' : 'stored'
  }
  for (const name of reset) {
    const fromEnv = name in fixtures.settingsDeployment
    const value = fromEnv
      ? fixtures.settingsDeployment[name]
      : (fixtures.settingsDefaults as Record<string, unknown>)[name] ?? null
    if (name in SECRETS) next[SECRETS[name as keyof typeof SECRETS]] = Boolean(value)
    else next[name] = value
    sources[name] = fromEnv ? 'env' : 'default'
  }
  // #426 — without their own key, render workers are handed the full one.
  next.render_key_fallback = Boolean(next.has_api_key) && !next.has_render_api_key
  next.sources = sources
  next.restart_required = Object.entries(fixtures.settingsApplies)
    .filter(([name, applies]) => applies === 'restart' && next[name] !== (state.running as Record<string, unknown>)[name])
    .map(([name]) => name)
  setMockSettings(next as Settings)
  return HttpResponse.json(mockSettings())
}

export const handlers = [
  http.get(`${base}/settings`, () => HttpResponse.json(mockSettings())),

  http.put(`${base}/settings`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    // Stored before the answer comes back, as the server commits before it responds.
    const response = putSettings(body)
    await delay(120)
    return response
  }),

  http.post(`${base}/settings/test`, async () => {
    await delay(200)
    if (!mockSettings().bambuddy_url?.startsWith('http')) {
      return problem(409, 'Conflict', 'no Bambuddy URL is configured')
    }
    if (!mockSettings().has_api_key) {
      return HttpResponse.json({
        ok: false,
        detail: "Bambuddy refused the API key when asked to list the printers. The key needs the 'Read Status' scope",
        printers: [],
      })
    }
    return HttpResponse.json({
      ok: true,
      detail: 'Connected. Bambuddy reports 3DP-31B-598.',
      printers: fixtures.targets.printers,
      scopes: [
        { scope: 'Read Status', status: 'ok', required: true, detail: 'Printers, their status, and the print history.' },
        ...(
          [
            ['Manage Library', true, 'Uploading 3MFs to the library, and its folders.'],
            ['Manage Queue', true, 'Queueing prints.'],
            ['Manage Projects', false, 'Sending to a Bambuddy project.'],
            ['Manage Archives', false, 'Attaching photos and timelapses to a print.'],
          ] as const
        ).map(([scope, required, what]) => ({
          scope,
          status: 'unknown',
          required,
          detail: `Not checked: Bambuddy cannot be asked what a key carries without a write, so a missing scope shows up when it is first used. Needed for: ${what}`,
        })),
      ],
    })
  }),

  // Each entry is forgotten through its own route; this lists them and forgets them all.
  http.get(`${base}/settings/remembered`, () => HttpResponse.json(mockRemembered())),

  http.delete(`${base}/settings/remembered`, () => {
    forgetMockRemembered()
    return HttpResponse.json(mockRemembered())
  }),

  http.get(`${base}/settings/bambuddy`, () => {
    if (!mockSettings().bambuddy_url) return problem(409, 'Conflict', 'no Bambuddy URL is configured')
    return HttpResponse.json(state.bambuddyStatus)
  }),
]
