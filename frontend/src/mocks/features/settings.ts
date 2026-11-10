import { HttpResponse, delay, http } from 'msw'
import type { BambuddyStatus, Settings } from '../../api/types'
import * as fixtures from '../fixtures'
import { forgetMockProjectTarget, forgetMockRemembered, mockRemembered, mockSettings, problem, setMockSettings } from '../handlers'

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
  store_s3_secret_access_key: 'has_store_s3_secret_access_key',
  git_remote_token: 'has_git_remote_token',
  storefront_turnstile_secret: 'has_storefront_turnstile_secret',
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
  'store_s3_endpoint',
  'store_s3_bucket',
  'store_s3_access_key_id',
  'store_s3_secret_access_key',
  'git_remote_url',
  'git_remote_username',
  'git_remote_token',
  // #5283 — the storefront's mail.
  'storefront_mail_host',
  'storefront_mail_user',
  'storefront_mail_sender',
  'storefront_turnstile_site_key',
  'storefront_turnstile_secret',
])
/** ISO 4217's codes with no minor unit (backend `orders/currency.py` `NO_MINOR_UNIT`). */
const NO_MINOR_UNIT = new Set(['XAG', 'XAU', 'XBA', 'XBB', 'XBC', 'XBD', 'XDR', 'XPD', 'XPT', 'XSU', 'XTS', 'XUA', 'XXX'])
/** #4250 — the price book's decimal strings (`core/settings.py` `decimal_string`). */
const PRICING_DECIMALS = new Set([
  'pricing_machine_rate_per_hour',
  'pricing_setup_fee',
  'pricing_labour_per_item',
  'pricing_markup_percent',
  'rough_estimate_factor',
  // #5326 — job cost.
  'pricing_labour_rate_per_hour',
  'pricing_usd_rate',
  'orders_thin_margin_percent',
  // #5281 — auto tax, below 100.
  'orders_tax_percent',
])

/** #5281 — `money.py` `DepositRule`'s checks, or the message of the first one a rule fails. */
function depositRuleProblem(rule: Record<string, unknown>): string | null {
  if (!['has_request', 'total_above', 'first_order'].includes(String(rule.criterion))) return "criterion: Input should be 'has_request', 'total_above' or 'first_order'"
  if ((rule.percent == null) === (rule.amount_minor == null)) return 'Value error, a rule has exactly one of percent and amount_minor'
  if ((rule.above_minor != null) !== (rule.criterion === 'total_above')) return 'Value error, above_minor is set exactly when the criterion is total_above'
  if (rule.percent != null && !(/^\d+(\.\d+)?$/.test(String(rule.percent)) && Number(rule.percent) > 0 && Number(rule.percent) <= 100)) return 'percent: Value error, a percent is above 0 and at most 100'
  if (rule.amount_minor != null && !(Number.isInteger(rule.amount_minor) && (rule.amount_minor as number) >= 1)) return 'amount_minor: Input should be greater than or equal to 1'
  if (rule.above_minor != null && !(Number.isInteger(rule.above_minor) && (rule.above_minor as number) >= 0)) return 'above_minor: Input should be greater than or equal to 0'
  return null
}

/** #5281 — `money.py` `ShippingOption`'s checks. */
function shippingOptionProblem(option: Record<string, unknown>): string | null {
  if (!/^[a-z0-9_-]{1,40}$/.test(String(option.id ?? ''))) return "id: String should match pattern '^[a-z0-9_-]{1,40}$'"
  const label = typeof option.label === 'string' ? [...option.label].length : 0
  if (label < 1 || label > 200) return 'label: String should have 1 to 200 characters'
  if (!(Number.isInteger(option.amount_minor) && (option.amount_minor as number) >= 0)) return 'amount_minor: Input should be greater than or equal to 0'
  if (typeof option.ships !== 'boolean') return 'ships: Input should be a valid boolean'
  return null
}
/** #5283 — the storefront mail's login and TLS mode, checked on the merged result. */
const MAIL_LOGIN_TLS = ['storefront_mail_user', 'storefront_mail_tls']
/** The settings the backend's `StoreNotReadyError` is decided from. */
const STORE_READINESS = [
  'store_backend',
  'library_backend',
  'bambuddy_url',
  'library_folder_id',
  'store_s3_endpoint',
  'store_s3_region',
  'store_s3_bucket',
  'store_s3_prefix',
  'store_s3_access_key_id',
  'store_s3_secret_access_key',
  'store_s3_path_style',
]
const AT_LEAST_ONE = new Set(['render_concurrency', 'check_concurrency', 'library_max_bytes'])
const MORE_THAN_ZERO = new Set([
  'render_timeout',
  'template_activity_max_timeout',
  'job_ttl',
  'media_upload_max_bytes',
  'read_max_objects',
  'read_max_visits',
  'read_max_triangles',
  'read_max_paint_digits',
])

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
    if (name === 'orders_deposit_policy' || name === 'storefront_shipping_options') {
      const rows = (value ?? []) as Record<string, unknown>[]
      if (rows.length > 20) return refused(name, 'List should have at most 20 items')
      const check = name === 'orders_deposit_policy' ? depositRuleProblem : shippingOptionProblem
      for (const [i, row] of rows.entries()) {
        const bad = check(row)
        if (bad) return refused(`${name}.${i}`, bad)
      }
      if (name === 'storefront_shipping_options' && new Set(rows.map((o) => o.id)).size < rows.length) return refused(name, 'Value error, shipping option ids are unique')
      // The view answers an empty list for null.
      next[name] = rows
      continue
    }
    if (name === 'storefront_payment_instructions' && typeof value === 'string' && value.length > 2000) return refused(name, 'String should have at most 2000 characters')
    if (!envSeeded.has(name)) {
      next[name] =
        name === 'display_unit'
          ? (value ?? 'mm')
          : name === 'asset_fetch_domains'
            ? (value ?? [...fixtures.DEFAULT_ASSET_FETCH_DOMAINS])
            : value
      continue
    }
    if (value === null && !NULLABLE.has(name)) {
      return refused(name, `Value error, ${envName(name)} cannot be cleared; reset it to follow the deployment's value`)
    }
    if (name === 'orders_currency' && !(typeof value === 'string' && /^[A-Z]{3}$/.test(value))) {
      return refused(name, "String should match pattern '^[A-Z]{3}$'")
    }
    // #4250 Ruling 5: ISO 4217 lists no minor unit for these, so nothing is priced in them.
    if (name === 'orders_currency' && NO_MINOR_UNIT.has(String(value))) {
      return refused(name, `Value error, ${envName(name)}: ${String(value)} has no minor unit in ISO 4217, so nothing can be priced in it`)
    }
    if (PRICING_DECIMALS.has(name)) {
      const text = typeof value === 'string' ? value.trim() : ''
      if (!/^\d+(\.\d+)?$/.test(text)) return refused(name, `Value error, ${envName(name)} must be at least 0`)
      if ((name === 'rough_estimate_factor' || name === 'pricing_usd_rate') && Number(text) <= 0) return refused(name, `Value error, ${envName(name)} must be more than 0`)
      if (name === 'orders_tax_percent' && Number(text) >= 100) return refused(name, `Value error, ${envName(name)} must be less than 100`)
    }
    if (name === 'pricing_rounding' && value !== 'to_minor_unit' && value !== 'to_major') {
      return refused(name, "Input should be 'to_minor_unit' or 'to_major'")
    }
    // #5283 — `core/settings.py`'s checks of the storefront's mail.
    if (name === 'storefront_mail_port' && !(Number.isInteger(value) && (value as number) >= 1 && (value as number) <= 65535)) {
      return refused(name, 'Input should be between 1 and 65535')
    }
    if (name === 'storefront_mail_sender' && typeof value === 'string' && !/^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$/.test(value.trim())) {
      return refused(name, `Value error, ${envName(name)} is an address, such as shop@example.com`)
    }
    if (name === 'storefront_mail_tls' && value !== 'starttls' && value !== 'implicit' && value !== 'none') {
      return refused(name, "Input should be 'starttls', 'implicit' or 'none'")
    }
    if (name === 'storefront_mail_host' && typeof value === 'string' && (/[\s/@]/.test(value.trim()) || !value.trim())) {
      return refused(name, `Value error, ${envName(name)} is a host name, such as smtp.example.com`)
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
  // #426 — `SettingsStore.save`: the merged settings may not leave the Bambuddy store
  // without its URL or inbox, whichever of them this save touched.
  if (
    STORE_READINESS.some((name) => name in body || reset.includes(name)) &&
    next.store_backend === 'bambuddy' &&
    (!next.bambuddy_url || next.library_folder_id === null || next.library_folder_id === undefined)
  ) {
    return problem(
      422,
      'Unprocessable Content',
      'the Bambuddy store needs a Bambuddy URL and a library folder (its inbox) saved first',
    )
  }
  // The S3 store's: its endpoint, bucket and both keys.
  if (
    STORE_READINESS.some((name) => name in body || reset.includes(name)) &&
    next.store_backend === 's3' &&
    (!next.store_s3_endpoint || !next.store_s3_bucket || !next.store_s3_access_key_id || !next.has_store_s3_secret_access_key)
  ) {
    return problem(
      422,
      'Unprocessable Content',
      'the S3 store needs its endpoint, bucket, access key id and secret access key saved first',
    )
  }
  // #3827 — `SettingsStore.save`: ScadBuddy's own library needs a content store.
  if (
    STORE_READINESS.some((name) => name in body || reset.includes(name)) &&
    next.library_backend === 'native' &&
    (next.store_backend ?? 'local') === 'local'
  ) {
    return problem(
      422,
      'Unprocessable Content',
      "ScadBuddy's own library keeps its files in the blob store: set store_backend to s3 or bambuddy first",
    )
  }
  // #5283 — `SettingsStore.save`: a login is never sent on a plain connection.
  if (
    MAIL_LOGIN_TLS.some((name) => name in body || reset.includes(name)) &&
    next.storefront_mail_user &&
    next.storefront_mail_tls === 'none'
  ) {
    return problem(
      422,
      'Unprocessable Content',
      'a plain connection (storefront_mail_tls none) is for a relay with no login: clear the user, or use starttls or implicit',
    )
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
            ['Manage Inventory', false, 'Recording which spool is in a tray, when you confirm it.'],
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

  http.delete(`${base}/settings/remembered/projects/:projectId`, ({ params }) => {
    forgetMockProjectTarget(String(params.projectId))
    return HttpResponse.json(mockRemembered())
  }),

  http.get(`${base}/settings/bambuddy`, () => {
    if (!mockSettings().bambuddy_url) return problem(409, 'Conflict', 'no Bambuddy URL is configured')
    return HttpResponse.json(state.bambuddyStatus)
  }),
]
