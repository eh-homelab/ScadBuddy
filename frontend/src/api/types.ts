/**
 * Wire types for the ScadBuddy API.
 *
 * Every one is an alias into `schema.d.ts`, which is generated from
 * `backend/openapi.json` by `pnpm gen:api`. Nothing here is hand-written against the
 * spec any more: the previous version was, and it disagreed with the running backend
 * in a dozen places (`api_key_set` vs `has_api_key`, `job_id` vs `id`, a `{x,y,z}`
 * bounding box that is really `{min,max,size}`, a thumbnail PUT that is multipart).
 * Regenerate after any backend change and `tsc` will point at whatever broke.
 */
import type { components } from './schema'

type Schemas = components['schemas']

export type ParamValue = boolean | number | string

export type Param = Schemas['Parameter']
export type ParamType = Param['type']
export type ParamOption = Schemas['Option']
export type CustomizerSchema = Schemas['CustomizerSchema']

export type ModelSummary = Schemas['ModelRecord']

export type Job = Schemas['JobStatus']
export type JobState = Job['status']
export type BoundingBox = Schemas['BoundingBox']
export type PartInfo = Schemas['PartInfo']
export type RenderAccepted = Schemas['RenderAccepted']

export type Output = Schemas['OutputDetail']

export type FontFamily = Schemas['FontFamily']
export type FontCatalogue = Schemas['FontCatalogueView']
export type CatalogueFont = Schemas['CatalogueEntry']
export type FontVariant = Schemas['FontVariant']
export type InstalledFamily = Schemas['InstalledFamily']

export type Settings = Schemas['SettingsView']
export type SettingsUpdate = Schemas['SettingsPatch']
export type ConnectionTest = Schemas['ConnectionTest']
export type BambuddyTargets = Schemas['BambuddyTargets']
export type BambuddyFolder = Schemas['Folder']
export type BambuddyPipeline = Schemas['Pipeline']
export type BambuddyPrinter = Schemas['Printer']
export type PresetRef = Schemas['PresetRef']
export type SidebarLink = Schemas['SidebarLink']

/** #86 — the print picker's own wire types. */
export type PipelineView = Schemas['PipelineView']
export type PipelineChoices = Schemas['PipelineChoices']
export type PipelineCreate = Schemas['PipelineCreate']
export type PipelineDefault = Schemas['PipelineDefault']
export type PresetChoice = Schemas['PresetChoice']
export type PresetOptions = Schemas['PresetOptions']
export type EligibilityOverview = Schemas['EligibilityOverview']
export type PipelineReport = Schemas['PipelineReport']
export type EligibilityReport = Schemas['EligibilityReport']
export type EligibilityIssue = Schemas['EligibilityIssue']
export type PerPrinterReport = Schemas['PerPrinterReport']
export type PrintRunRequest = Schemas['PrintRunRequest']
export type PrintRunResult = Schemas['PrintRunResult']

/**
 * #89 — run tracking.
 *
 * These two are the only hand-written wire types left, and they are hand-written for a
 * mechanical reason rather than a judgement: `backend/openapi.json` carries
 * `PrintProgress` and `CopyProgress`, but `schema.d.ts` in this tree was generated
 * before the #89 backend landed and does not. The shapes below are transcribed from
 * that spec — including which members `openapi-typescript` makes required, namely the
 * ones carrying a default — so once `pnpm gen:api` has run each becomes a one-line
 * alias like every other type here.
 */

/** Which of Bambuddy's two routes the print left by. They report progress differently. */
export type PrintRoute = 'pipeline' | 'slice_queue'

/**
 * `unknown` is a real state, not a parse failure: Bambuddy's status vocabularies differ
 * per object, so a value the backend has not seen renders as "still going" rather than
 * silently as "done".
 */
export type PrintStage =
  | 'pending'
  | 'running'
  | 'queued'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'unknown'

export interface CopyProgress {
  copy_index?: number | null
  message?: string | null
  printer_name?: string | null
  queue_entry_id?: number | null
  stage: PrintStage
  /** Why it is not printing *yet*. Waiting is not failing — `message` is the failure. */
  waiting_reason?: string | null
}

export interface PrintProgress {
  bambuddy_url: string
  copies: number
  copies_cancelled: number
  copies_completed: number
  copies_detail?: CopyProgress[]
  copies_failed: number
  copies_in_progress: number
  /** Bambuddy's own failure text, verbatim. */
  error_message?: string | null
  /** What to do about it, chosen by the backend from *where* it failed, not the wording. */
  fix?: string | null
  pipeline_run_id?: number | null
  queue_item_id?: number | null
  route: PrintRoute
  /** The only thing that says polling can stop. Never re-derive it from `stage`. */
  settled: boolean
  slice_job_id?: number | null
  stage: PrintStage
}

export type SendRequest = Schemas['SendRequest']
export type SendResult = Schemas['SendResult']
export type SendMode = SendResult['mode']

/**
 * A view model, not a wire type: the API returns a flat `parameters` list plus the
 * group names in source order, and the panel wants them bucketed.
 */
export interface ParamGroup {
  name: string
  params: Param[]
}

/**
 * RFC 9457 problem details. The backend adds extensions alongside the standard
 * members — `required_scope` on a missing Bambuddy scope, `bambuddy_body` carrying a
 * pipeline-eligibility report verbatim — so unknown keys are kept, not dropped.
 */
export interface Problem {
  type?: string
  title: string
  status: number
  detail?: string
  instance?: string
  required_scope?: string
  bambuddy_status?: number
  bambuddy_body?: unknown
  [extension: string]: unknown
}
