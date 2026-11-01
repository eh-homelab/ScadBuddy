import type { Settings } from '../../api/types'
import { RUNTIME_FIELDS, envVar, serverValue, type FieldName, type FieldSpec, type SectionId } from './fields'

/** #296 — `used` of `limit`, where a limit of 0 means none. */
export function ofLimit(used: string, limit: number, format: (n: number) => string): string {
  return limit > 0 ? `${used} of ${format(limit)}` : `${used} (no limit)`
}

export const SECRETS = [
  'bambuddy_api_key',
  'bambuddy_render_api_key',
  'google_fonts_api_key',
  'store_s3_secret_access_key',
] as const
export type Secret = (typeof SECRETS)[number]

export function isSecret(name: FieldName): name is Secret {
  return (SECRETS as readonly string[]).includes(name)
}

/** The flag each secret is reported under. */
const SECRET_FLAGS: Record<Secret, keyof Settings> = {
  bambuddy_api_key: 'has_api_key',
  bambuddy_render_api_key: 'has_render_api_key',
  google_fonts_api_key: 'has_google_fonts_api_key',
  store_s3_secret_access_key: 'has_store_s3_secret_access_key',
}

/**
 * #4803 — a copy of `settings` with `names` (their values, secret flags and sources) taken
 * from `from`. Always a new object, so a seed queued with it is applied even when nothing moved.
 */
export function patchFields(settings: Settings, from: Settings, names: readonly FieldName[]): Settings {
  const out: Record<string, unknown> = { ...settings }
  const sources = { ...settings.sources }
  for (const name of names) {
    const key = isSecret(name) ? SECRET_FLAGS[name] : name
    out[key] = (from as Record<string, unknown>)[key]
    const source = from.sources?.[name]
    if (source === undefined) delete sources[name]
    else sources[name] = source
  }
  out.sources = sources
  return out as Settings
}

const ID_FIELDS: readonly FieldName[] = ['library_folder_id', 'printer_id', 'last_project_id']

/** The fields each section saves. The runtime ones come from `RUNTIME_FIELDS`. */
export const HAND_LAID: Partial<Record<SectionId, readonly FieldName[]>> = {
  connection: ['bambuddy_url', 'bambuddy_web_urls', 'bambuddy_api_key', 'bambuddy_render_api_key', 'public_url'],
  printing: ['printer_id', 'print_run_retention_seconds', 'operation_retention_seconds'],
  // #426 — the blob store beside the inbox folder it needs.
  projects: [
    'library_folder_id',
    'last_project_id',
    'destination_sort',
    'store_backend',
    'library_backend',
    // Plan 2026-10-16 Task 14: who slices, beside where print files are kept.
    'slicer_backend',
    // The S3 store's, under the Blob store choice.
    'store_s3_endpoint',
    'store_s3_region',
    'store_s3_bucket',
    'store_s3_prefix',
    'store_s3_access_key_id',
    'store_s3_secret_access_key',
    'store_s3_path_style',
  ],
  preview: ['display_unit', 'default_plate'],
  administration: ['activity_follow', 'activity_ttl_seconds', 'activity_repair_window_seconds'],
  // #959 — where the agent's `fetch_asset` (and `POST /models/{slug}/assets/fetch`) may fetch from.
  uploads: ['asset_fetch_domains'],
}

export const SPECS: Record<string, FieldSpec> = Object.fromEntries(RUNTIME_FIELDS.map((spec) => [spec.name, spec]))

/**
 * An env-seeded field this page has no control for yet is still shown and editable, in
 * Diagnostics, so no setting the server takes is out of reach (#322).
 */
export function extraSpecs(settings: Settings | undefined): FieldSpec[] {
  const known = new Set<string>([...Object.values(HAND_LAID).flat(), ...Object.keys(SPECS)])
  return Object.keys(settings?.sources ?? {})
    .filter((name) => !known.has(name))
    .map((name) => {
      const current = (settings as Record<string, unknown>)[name]
      return {
        name: name as FieldName,
        section: 'diagnostics' as const,
        label: envVar(name),
        kind:
          typeof current === 'boolean'
            ? ('bool' as const)
            : typeof current === 'number'
              ? ('count' as const)
              : ('text' as const),
      }
    })
}

export function fieldsOf(section: SectionId, settings: Settings | undefined): FieldName[] {
  return [
    ...(HAND_LAID[section] ?? []),
    ...[...RUNTIME_FIELDS, ...extraSpecs(settings)]
      .filter((spec) => spec.section === section)
      .map((spec) => spec.name),
  ]
}

/** A day, in the seconds the retentions are stored in (#1052, #1053). */
export const DAY_SECONDS = 86400
/** The settings shown in days and stored in seconds; empty keeps every row. */
export const RETENTIONS: readonly FieldName[] = ['print_run_retention_seconds', 'operation_retention_seconds']

/**
 * The stored activity settings (not env-seeded, so no badge or reset): empty is `null`,
 * the default, shown as the placeholder. The bounds are the backend's
 * (`settings_store.py`), checked here so a bad value is not a 422.
 */
/** The longest retention the API takes, ten years (`settings_store.RETENTION_MAX_SECONDS`). */
export const RETENTION_MAX_DAYS = 3650
export const ACTIVITY_TTL = { min: 86400, max: RETENTION_MAX_DAYS * 86400, default: 2_592_000 }
export const ACTIVITY_REPAIR = { min: 600, max: 7 * 86400, default: 86400 }
/** `activity_follow` empty is the default (`["browser"]`); this word follows nobody (`[]`). */
export const FOLLOW_NOBODY = 'none'
/**
 * #959 — `asset_fetch_domains` is shown resolved, a domain a line: empty saves `null`
 * (the server's defaults, shown again once saved) and this word saves `[]`, fetching from
 * nowhere. The server normalises each domain and names one it refuses.
 */
export const FETCH_NOWHERE = 'none'

export function baseline(settings: Settings, name: FieldName): string {
  if (name === 'display_unit') return settings.display_unit ?? 'mm'
  if (name === 'destination_sort') return settings.destination_sort ?? 'touched'
  if (name === 'activity_follow') {
    const follow = settings.activity_follow
    return follow == null ? '' : follow.length === 0 ? FOLLOW_NOBODY : follow.join(', ')
  }
  if (name === 'asset_fetch_domains') {
    const domains = settings.asset_fetch_domains ?? []
    return domains.length === 0 ? FETCH_NOWHERE : domains.join('\n')
  }
  if (RETENTIONS.includes(name)) {
    const seconds = settings[name as 'print_run_retention_seconds' | 'operation_retention_seconds']
    return seconds == null ? '' : String(seconds / DAY_SECONDS)
  }
  return serverValue(settings, name)
}

export type Problem = { problem: string }

/** The form value for `name`, as `PUT /settings` takes it, or a problem to show. */
export function toPatchValue(name: FieldName, raw: string, settings: Settings): unknown {
  if (ID_FIELDS.includes(name)) return raw === '' ? null : Number(raw)
  if (
    name === 'display_unit' ||
    name === 'destination_sort' ||
    name === 'library_backend' ||
    name === 'slicer_backend'
  )
    return raw
  if (name === 'store_s3_path_style') return raw === 'true'
  if (RETENTIONS.includes(name)) {
    // Empty keeps every row; a number of days prunes the older ones.
    if (raw.trim() === '') return null
    const days = Number(raw)
    if (!Number.isFinite(days) || days < 1 || days > RETENTION_MAX_DAYS) {
      return { problem: `Enter 1 to ${RETENTION_MAX_DAYS} days, or leave it empty to keep every one.` } satisfies Problem
    }
    return days * DAY_SECONDS
  }
  if (name === 'activity_follow') {
    const items = raw.split(',').map((one) => one.trim()).filter((one) => one !== '')
    if (items.length === 0) return null
    if (items.length === 1 && items[0] === FOLLOW_NOBODY) return []
    return items
  }
  if (name === 'asset_fetch_domains') {
    const items = [...new Set(raw.split(/[\s,]+/).filter((one) => one !== ''))]
    if (items.length === 0) return null
    if (items.length === 1 && items[0] === FETCH_NOWHERE) return []
    return items
  }
  if (name === 'activity_ttl_seconds' || name === 'activity_repair_window_seconds') {
    if (raw.trim() === '') return null
    const seconds = Number(raw)
    const { min, max } = name === 'activity_ttl_seconds' ? ACTIVITY_TTL : ACTIVITY_REPAIR
    if (!Number.isFinite(seconds)) return { problem: 'Enter a number of seconds, or leave it empty for the default.' } satisfies Problem
    if (seconds < min || seconds > max) {
      return { problem: `Enter ${min} to ${max} seconds.` } satisfies Problem
    }
    return seconds
  }
  const kind = SPECS[name]?.kind ?? extraSpecs(settings).find((spec) => spec.name === name)?.kind
  if (kind === 'bool') return raw === 'true'
  if (kind === 'seconds' || kind === 'count' || kind === 'bytes') {
    if (raw.trim() === '') return { problem: 'Enter a value, or reset it to the deployment value.' } satisfies Problem
    const number = Number(raw)
    if (!Number.isFinite(number)) return { problem: 'Enter a number.' } satisfies Problem
    // #2348 — none of them takes a negative, and a count is whole; the API checks each
    // field's own bounds too and names the field it refuses.
    if (number < 0) return { problem: 'Enter 0 or more.' } satisfies Problem
    if (kind === 'count' && !Number.isInteger(number)) return { problem: 'Enter a whole number.' } satisfies Problem
    return number
  }
  // Text: empty clears it, which beats the deployment's value until it is reset.
  return raw === '' ? null : raw
}

export function isProblem(value: unknown): value is Problem {
  return typeof value === 'object' && value !== null && 'problem' in value
}


export type StoreChoice = {
  backend: 'local' | 'bambuddy' | 's3'
  savedBambuddyUrl: boolean
  bambuddyReady: boolean
  s3Ready: boolean
}

/**
 * #426 — the Bambuddy store needs a SAVED Bambuddy URL and an inbox folder in the form:
 * until then the Blob store choice shows, sends and compares the local store. The URL is
 * the saved one, never Connection's unsaved draft: a Projects & files save cannot commit
 * it, so what the choice shows and what that save sends always agree.
 */
export function storeChoice(settings: Settings, value: (name: FieldName) => string): StoreChoice {
  const savedBambuddyUrl = baseline(settings, 'bambuddy_url') !== ''
  const bambuddyReady = savedBambuddyUrl && value('library_folder_id') !== ''
  // The S3 store needs its SAVED endpoint, bucket and both keys, as the server does.
  const savedS3 = (name: FieldName) => baseline(settings, name) !== ''
  const s3Ready =
    savedS3('store_s3_endpoint') &&
    savedS3('store_s3_bucket') &&
    savedS3('store_s3_access_key_id') &&
    Boolean(settings.has_store_s3_secret_access_key)
  const chosen = value('store_backend')
  const backend =
    chosen === 's3' ? (s3Ready ? 's3' : 'local') : bambuddyReady && chosen === 'bambuddy' ? 'bambuddy' : 'local'
  return { backend, savedBambuddyUrl, bambuddyReady, s3Ready }
}
