import type { Settings, SettingsUpdate } from '../../api/types'

/**
 * #322 — the Settings page's sections, in page order. `id` is the in-page anchor.
 */
export const SECTIONS = [
  { id: 'connection', title: 'Connection' },
  { id: 'printing', title: 'Printing defaults' },
  { id: 'projects', title: 'Projects & files' },
  { id: 'uploads', title: 'Uploads' },
  { id: 'rendering', title: 'Rendering' },
  { id: 'fonts', title: 'Fonts' },
  { id: 'libraries', title: 'Libraries' },
  { id: 'preview', title: 'Preview' },
  { id: 'remembered', title: 'Remembered choices' },
  { id: 'assistant', title: 'Assistant' },
  { id: 'diagnostics', title: 'Diagnostics' },
  { id: 'administration', title: 'Administration' },
  { id: 'about', title: 'About' },
] as const

export type SectionId = (typeof SECTIONS)[number]['id']

export function sectionTitle(id: SectionId): string {
  return SECTIONS.find((section) => section.id === id)?.title ?? id
}

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

export type FieldSpec = {
  name: FieldName
  section: SectionId
  label: string
  kind: FieldKind
  help?: string
  /** In a section's "Advanced" disclosure. */
  advanced?: boolean
}

/**
 * The runtime settings (#322): each is env-seeded, so it carries a source badge and a
 * reset. The ones with their own controls on the page (the Bambuddy pickers, the plate,
 * the unit) are not listed here; they are laid out by hand.
 */
export const RUNTIME_FIELDS: readonly FieldSpec[] = [
  {
    name: 'temporal_ui_url',
    section: 'administration',
    label: 'Temporal UI URL',
    kind: 'url',
    help: 'The Temporal web UI, where render workflows can be inspected. Empty shows no link.',
  },
  {
    name: 'media_upload_max_bytes',
    section: 'uploads',
    label: 'Largest media upload',
    kind: 'bytes',
    help: 'For template videos and print photos. The ingress in front of ScadBuddy has its own body limit; a value above it fails there instead.',
  },
  {
    name: 'asset_max_total_bytes',
    section: 'uploads',
    label: 'File parameter uploads, total size',
    kind: 'bytes',
    help: 'The SVGs and PNGs attached to file parameters. 0 is no limit.',
  },
  {
    name: 'asset_max_count',
    section: 'uploads',
    label: 'File parameter uploads, count',
    kind: 'count',
    help: '0 is no limit.',
  },
  {
    name: 'asset_sweep_grace',
    section: 'uploads',
    label: 'Keep an unused upload for',
    kind: 'seconds',
    help: 'An upload no output, preset or render uses is removed after this long. At least an hour.',
  },
  {
    name: 'asset_sweep_interval',
    section: 'uploads',
    label: 'Sweep unused uploads every',
    kind: 'seconds',
    help: '0 turns the sweep off.',
  },
  {
    name: 'library_max_bytes',
    section: 'uploads',
    label: 'Largest OpenSCAD library checkout',
    kind: 'bytes',
  },
  {
    name: 'render_timeout',
    section: 'rendering',
    label: 'Render timeout',
    kind: 'seconds',
    help: 'How long one openscad run may take before it is stopped.',
  },
  {
    name: 'template_activity_max_timeout',
    section: 'rendering',
    label: 'Template activity limit',
    kind: 'seconds',
    help: "The longest a template pipeline's own activity may run; a whole pipeline gets four times this.",
  },
  {
    name: 'job_ttl',
    section: 'rendering',
    label: 'Keep finished renders for',
    kind: 'seconds',
  },
  {
    name: 'render_concurrency',
    section: 'rendering',
    label: 'Renders at once',
    kind: 'count',
  },
  {
    name: 'solid_concurrency',
    section: 'rendering',
    label: 'Colours rendered at once, per job',
    kind: 'count',
    help: '0 works it out from the CPUs this container may use.',
  },
  {
    name: 'check_concurrency',
    section: 'rendering',
    label: 'Editor checks at once',
    kind: 'count',
  },
  {
    name: 'lsp_sessions',
    section: 'rendering',
    label: 'Editor language servers',
    kind: 'count',
    help: 'One per open editor. Past it, an editor goes without completion and hover.',
  },
  {
    name: 'render_queue_max',
    section: 'rendering',
    label: 'Refuse renders past this many waiting',
    kind: 'count',
    help: '0 accepts every render.',
  },
  {
    name: 'preview_renders',
    section: 'rendering',
    label: 'Render a preview for models with no thumbnail',
    kind: 'bool',
  },
  {
    name: 'render_queue_depth_slo',
    section: 'rendering',
    label: 'Queue depth objective',
    kind: 'count',
    help: 'Exported for alerts; nothing is refused on reaching it.',
    advanced: true,
  },
  {
    name: 'render_latency_slo',
    section: 'rendering',
    label: 'Render latency objective',
    kind: 'seconds',
    help: 'Exported for alerts; nothing is refused on reaching it.',
    advanced: true,
  },
  {
    name: 'duplicate_staging_max_age',
    section: 'rendering',
    label: 'Treat an unfinished duplicate as abandoned after',
    kind: 'seconds',
    advanced: true,
  },
  {
    name: 'google_fonts_api_key',
    section: 'fonts',
    label: 'Google Fonts API key',
    kind: 'secret',
    help: 'Optional. Without one the catalogue comes from fonts.google.com. A new key refetches the catalogue.',
  },
  {
    name: 'fonts_catalogue_ttl',
    section: 'fonts',
    label: 'Refresh the font catalogue after',
    kind: 'seconds',
  },
  {
    name: 'log_level',
    section: 'diagnostics',
    label: 'Log level',
    kind: 'level',
  },
  {
    name: 'event_log_retention_seconds',
    section: 'diagnostics',
    label: 'Keep live-update events for',
    kind: 'seconds',
    help: 'For replay after a short disconnect. 0 is no limit.',
  },
  {
    name: 'event_log_retention_rows',
    section: 'diagnostics',
    label: 'Keep at most this many live-update events',
    kind: 'count',
    help: '0 is no limit.',
  },
  {
    name: 'realtime_sockets',
    section: 'diagnostics',
    label: 'Live-update connections',
    kind: 'count',
    help: 'One per open tab.',
  },
]

export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const

/** The key a secret is reported under: `has_api_key`, `has_google_fonts_api_key`. */
export function hasKey(name: 'bambuddy_api_key' | 'google_fonts_api_key'): 'has_api_key' | 'has_google_fonts_api_key' {
  return name === 'bambuddy_api_key' ? 'has_api_key' : 'has_google_fonts_api_key'
}

export function envVar(name: string): string {
  return `SCADBUDDY_${name.toUpperCase()}`
}

/** The value a field has on the server, as the form holds it. Secrets are never shown. */
export function serverValue(settings: Settings, name: FieldName): string {
  if (name === 'bambuddy_api_key' || name === 'bambuddy_render_api_key' || name === 'google_fonts_api_key') return ''
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
