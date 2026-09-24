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
/**
 * The intersection is the same stale-generator stand-in as the #79 block below: the
 * backend takes `project_id` on a run, and the generated type has not caught up. Drop it
 * when `schema.d.ts` is regenerated.
 */
export type PrintRunRequest = Schemas['PrintRunRequest'] & { project_id?: number | null }
export type PrintRunResult = Schemas['PrintRunResult']

/**
 * #79 — projects.
 *
 * These are the only hand-written wire types left in this file, and they are a stand-in,
 * not a new habit. `backend/openapi.json` carries every one of them, but
 * `src/api/schema.d.ts` has not been regenerated on this branch — and regenerating it
 * pulls in #87's and #89's backend commits too, which make `PrintRunResult.run` nullable
 * and so stop `PrintPicker` type-checking. That file is being edited on two other
 * branches, so the generator has to run once all three land. Replace each of these with a
 * `Schemas['...']` alias in that same commit; the shapes below are exactly what
 * `openapi-typescript` emits for them, so the swap is a no-op.
 */

/** `folder_id` is null for a project with no library folder yet — one made outside
 * ScadBuddy usually has none, and linking it creates the folder rather than refusing. */
export interface ProjectView {
  archive_count: number
  colour?: string | null
  description?: string | null
  folder_id?: number | null
  folder_name?: string | null
  id: number
  name: string
  queue_count: number
  status: string
}

export interface ProjectChoices {
  model_project_id?: number | null
  projects?: ProjectView[]
}

/** `project_id` set links that project; otherwise `name` is required and one is created.
 * Bambuddy's own `ProjectCreate` carries `target_count`, `due_date` and `budget` as well,
 * and ScadBuddy sends none of them rather than inventing numbers for its project page. */
export interface ProjectRequest {
  colour?: string | null
  description?: string | null
  folder_id?: number | null
  name?: string | null
  project_id?: number | null
  tags?: string | null
  url?: string | null
}

export interface ModelProject {
  project_id?: number | null
  slug: string
}

export interface ProjectAttach {
  project_id?: number | null
  queue_item_ids?: number[]
}

export interface AttachResult {
  archive_ids?: number[]
  project_id: number
  queue_item_ids?: number[]
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
