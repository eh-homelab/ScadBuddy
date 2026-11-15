import type { Settings, SettingsUpdate } from '../../api/types'

/**
 * #2171 — the Settings sections that save runtime fields (`RUNTIME_FIELDS`, `HAND_LAID`).
 * `printers` and `inventory` own none, so they are not here.
 */
export type SectionId =
  | 'connection'
  | 'printing'
  | 'print-watcher'
  | 'orders'
  | 'projects'
  | 'uploads'
  | 'rendering'
  | 'fonts'
  | 'git-remote'
  | 'storefront-mail'
  | 'storefront-pricing'
  | 'preview'
  | 'diagnostics'
  | 'administration'

/** How a field is edited. Every value travels as a string in the form. */
export type FieldKind =
  | 'text'
  | 'url'
  | 'secret'
  | 'id'
  | 'select'
  | 'seconds'
  | 'count'
  | 'bytes'
  | 'bool'
  | 'level'

/** Every field `PUT /settings` takes, other than `reset`. */
export type FieldName = Exclude<keyof SettingsUpdate, 'reset'>

/**
 * Where a field is laid out and how it is edited. Its label and help are not here: they
 * are the backend's (#887, `core/setting_docs.py`), read with `docOf`.
 */
export type FieldSpec = {
  name: FieldName
  section: SectionId
  kind: FieldKind
  /** In a section's "Advanced" disclosure. */
  advanced?: boolean
  /** A `select`'s choices. */
  choices?: readonly { value: string; label: string }[]
}

/**
 * The runtime settings (#322): each is env-seeded, so it carries a source badge and a
 * reset. The ones with their own controls on the page (the Bambuddy pickers, the plate,
 * the unit) are not listed here; they are laid out by hand.
 */
export const RUNTIME_FIELDS: readonly FieldSpec[] = [
  {
    name: 'orders_currency',
    section: 'orders',
    kind: 'text',
  },
  // #4250 — the price book's policy (spec 2026-10-18 §5.1): read by each quote and copied
  // into its lines, so a change never moves a recorded quote.
  {
    name: 'pricing_machine_rate_per_hour',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'pricing_setup_fee',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'pricing_labour_per_item',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'pricing_markup_percent',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'pricing_rounding',
    section: 'orders',
    kind: 'select',
    choices: [
      { value: 'to_minor_unit', label: 'Each line to the cent' },
      { value: 'to_major', label: 'Up to a whole unit' },
    ],
  },
  {
    name: 'rough_estimate_factor',
    section: 'orders',
    kind: 'text',
    advanced: true,
  },
  // #5326 — job cost (spec 2026-11-05 §4.3, §4.4, §5), in the shop's currency.
  {
    name: 'pricing_labour_rate_per_hour',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'orders_thin_margin_percent',
    section: 'orders',
    kind: 'text',
  },
  // #5281 — auto tax (storefront spec §9): one `tax` charge ScadBuddy computes on an open order.
  {
    name: 'orders_tax_percent',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'orders_tax_shipping',
    section: 'orders',
    kind: 'bool',
  },
  // #6133 — the handling fee on the things the shop buys for a customer (spec 2026-11-13 §10.3).
  {
    name: 'orders_purchase_fee_flat_minor',
    section: 'orders',
    kind: 'count',
  },
  {
    name: 'orders_purchase_fee_percent',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'pricing_usd_rate',
    section: 'orders',
    kind: 'text',
    advanced: true,
  },
  // #5283 — the storefront's bot check (storefront spec §5.8): Cloudflare Turnstile on
  // design requests, submit, sign-in code requests and sign-ins, on while a site key
  // is set.
  {
    name: 'storefront_turnstile_site_key',
    section: 'orders',
    kind: 'text',
  },
  {
    name: 'storefront_turnstile_secret',
    section: 'orders',
    kind: 'secret',
  },
  // #4939 — a native run's plate-clear wait, as Bambuddy's queue's `require_plate_clear`.
  {
    name: 'lan_require_plate_clear',
    section: 'printing',
    kind: 'bool',
  },
  // #4953 — whether a fulfilled line item may be unfulfilled (#4952), by a person or the agent.
  {
    name: 'orders_allow_unfulfil',
    section: 'orders',
    kind: 'bool',
  },
  {
    name: 'temporal_ui_url',
    section: 'administration',
    kind: 'url',
  },
  {
    name: 'spool_overlay_refresh_seconds',
    section: 'administration',
    kind: 'seconds',
  },
  {
    name: 'media_upload_max_bytes',
    section: 'uploads',
    kind: 'bytes',
  },
  {
    name: 'asset_max_total_bytes',
    section: 'uploads',
    kind: 'bytes',
  },
  {
    name: 'asset_max_count',
    section: 'uploads',
    kind: 'count',
  },
  {
    name: 'asset_sweep_grace',
    section: 'uploads',
    kind: 'seconds',
  },
  {
    name: 'asset_sweep_interval',
    section: 'uploads',
    kind: 'seconds',
  },
  {
    name: 'read_max_triangles',
    section: 'projects',
    kind: 'count',
    advanced: true,
  },
  {
    name: 'read_max_objects',
    section: 'projects',
    kind: 'count',
    advanced: true,
  },
  {
    name: 'read_max_visits',
    section: 'projects',
    kind: 'count',
    advanced: true,
  },
  {
    name: 'read_max_paint_digits',
    section: 'projects',
    kind: 'count',
    advanced: true,
  },
  {
    name: 'library_max_bytes',
    section: 'uploads',
    kind: 'bytes',
  },
  {
    name: 'render_timeout',
    section: 'rendering',
    kind: 'seconds',
  },
  {
    name: 'template_activity_max_timeout',
    section: 'rendering',
    kind: 'seconds',
  },
  {
    name: 'job_ttl',
    section: 'rendering',
    kind: 'seconds',
  },
  {
    name: 'render_concurrency',
    section: 'rendering',
    kind: 'count',
  },
  {
    name: 'solid_concurrency',
    section: 'rendering',
    kind: 'count',
  },
  {
    name: 'check_concurrency',
    section: 'rendering',
    kind: 'count',
  },
  {
    name: 'lsp_sessions',
    section: 'rendering',
    kind: 'count',
  },
  {
    name: 'render_queue_max',
    section: 'rendering',
    kind: 'count',
  },
  {
    name: 'preview_renders',
    section: 'rendering',
    kind: 'bool',
  },
  {
    name: 'render_queue_depth_slo',
    section: 'rendering',
    kind: 'count',
    advanced: true,
  },
  {
    name: 'render_latency_slo',
    section: 'rendering',
    kind: 'seconds',
    advanced: true,
  },
  {
    name: 'duplicate_staging_max_age',
    section: 'rendering',
    kind: 'seconds',
    advanced: true,
  },
  {
    name: 'flow_approval_timeout_seconds',
    section: 'rendering',
    kind: 'seconds',
    advanced: true,
  },
  {
    name: 'google_fonts_api_key',
    section: 'fonts',
    kind: 'secret',
  },
  {
    name: 'fonts_catalogue_ttl',
    section: 'fonts',
    kind: 'seconds',
  },
  // #4974 — the off-box copy of the model and flow history.
  {
    name: 'git_remote_url',
    section: 'git-remote',
    kind: 'url',
  },
  {
    name: 'git_remote_username',
    section: 'git-remote',
    kind: 'text',
  },
  {
    name: 'git_remote_token',
    section: 'git-remote',
    kind: 'secret',
  },
  // #5283 — the storefront's mail, sent by the CRUD service. The password is not a
  // setting: it is sealed apart (`StorefrontMailPassword`).
  {
    name: 'storefront_mail_host',
    section: 'storefront-mail',
    kind: 'text',
  },
  {
    name: 'storefront_mail_port',
    section: 'storefront-mail',
    kind: 'count',
  },
  {
    name: 'storefront_mail_user',
    section: 'storefront-mail',
    kind: 'text',
  },
  {
    name: 'storefront_mail_sender',
    section: 'storefront-mail',
    kind: 'text',
  },
  {
    name: 'storefront_mail_tls',
    section: 'storefront-mail',
    kind: 'select',
    choices: [
      { value: 'starttls', label: 'STARTTLS on the port (usually 587)' },
      { value: 'implicit', label: 'Implicit TLS (usually 465)' },
      { value: 'none', label: 'None, a relay with no login' },
    ],
  },
  // #6153 — the storefront's slices at once; the shop slice profile is saved on its own.
  {
    name: 'storefront_slices_concurrent',
    section: 'storefront-pricing',
    kind: 'count',
  },
  {
    name: 'log_level',
    section: 'diagnostics',
    kind: 'level',
  },
  {
    name: 'event_log_retention_seconds',
    section: 'diagnostics',
    kind: 'seconds',
  },
  {
    name: 'event_log_retention_rows',
    section: 'diagnostics',
    kind: 'count',
  },
  {
    name: 'realtime_sockets',
    section: 'diagnostics',
    kind: 'count',
  },
]

export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const

const HAS_KEY = {
  bambuddy_api_key: 'has_api_key',
  bambuddy_render_api_key: 'has_render_api_key',
  google_fonts_api_key: 'has_google_fonts_api_key',
  git_remote_token: 'has_git_remote_token',
  storefront_turnstile_secret: 'has_storefront_turnstile_secret',
} as const
export type SecretName = keyof typeof HAS_KEY

/** The flag a secret is reported under: `has_api_key`, `has_render_api_key`, … */
export function hasKey(name: SecretName): (typeof HAS_KEY)[SecretName] {
  return HAS_KEY[name]
}

/** #887 — a field's label, help and env var, as `GET /settings` gives them in `docs`. */
export type FieldDoc = { label: string; help?: string; env?: string }

/**
 * The label and help of `name` from the backend's docs, and its env var when one seeds it.
 * A field the docs do not know (an older server) is labelled by its name. So does every
 * field when the answer has no `docs`: a test that serves `/api/v1/settings` by hand must
 * spread the `settings` fixture or add `docs: settingDocs` (both `mocks/fixtures.ts`), or
 * its lookups by label miss.
 */
export function docOf(settings: Settings | undefined, name: string): FieldDoc {
  const doc = settings?.docs?.[name]
  if (!doc) return { label: name }
  return { label: doc.title, help: doc.description, env: doc.env_var ?? undefined }
}

export function envVar(name: string): string {
  return `SCADBUDDY_${name.toUpperCase()}`
}

/** The value a field has on the server, as the form holds it. Secrets are never shown. */
export function serverValue(settings: Settings, name: FieldName): string {
  if (
    name === 'bambuddy_api_key' ||
    name === 'bambuddy_render_api_key' ||
    name === 'google_fonts_api_key' ||
    name === 'store_s3_secret_access_key' ||
    name === 'git_remote_token' ||
    name === 'storefront_turnstile_secret'
  )
    return ''
  const value = (settings as Record<string, unknown>)[name]
  if (value === null || value === undefined) return ''
  return String(value)
}

/**
 * Decimal and binary units both: the defaults are written either way (the media limit is
 * 1 GiB, the upload store's cap 1 GB), and each is shown in the unit that holds it exactly.
 * A value that is a whole number of none of them is shown in bytes, so it round-trips.
 */
export const BYTE_UNITS = {
  B: 1,
  MB: 1_000_000,
  GB: 1_000_000_000,
  MiB: 1024 * 1024,
  GiB: 1024 * 1024 * 1024,
} as const
export type ByteUnit = keyof typeof BYTE_UNITS

export function bestUnit(bytes: number): ByteUnit {
  if (bytes === 0) return 'MB'
  for (const unit of ['GiB', 'GB', 'MiB', 'MB'] as const) {
    if (bytes >= BYTE_UNITS[unit] && bytes % BYTE_UNITS[unit] === 0) return unit
  }
  return 'B'
}

export function inUnit(bytes: number, unit: ByteUnit): string {
  return String(Number((bytes / BYTE_UNITS[unit]).toFixed(3)))
}

/** A rough reading of seconds, beside the number: "1 day", "2 h", "90 s". */
export function humanSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  const units: [number, string][] = [
    [86400, 'day'],
    [3600, 'h'],
    [60, 'min'],
  ]
  for (const [size, name] of units) {
    if (seconds >= size) {
      const value = Number((seconds / size).toFixed(1))
      return name === 'day' ? `${value} ${value === 1 ? 'day' : 'days'}` : `${value} ${name}`
    }
  }
  return `${seconds} s`
}
