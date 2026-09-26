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
export type SourceCheck = Schemas['SourceCheck']
export type Diagnostic = Schemas['Diagnostic']

/**
 * Body for creating a model from pasted source. Hand-written because the route
 * accepts three content types and FastAPI inlines this one's schema in the
 * document rather than naming it under `components`.
 */
export interface PastedSource {
  name: string
  source: string
  description?: string
  tags?: string[]
  force?: boolean
}

/** #90 — one entry of a model's git history, and a patch between two of them. */
export type ModelVersion = Schemas['ModelVersion']
export type VersionFile = Schemas['VersionFile']
export type VersionDiff = Schemas['VersionDiff']

export type Job = Schemas['JobStatus']
export type JobState = Job['status']
export type BoundingBox = Schemas['BoundingBox']
export type PartInfo = Schemas['PartInfo']
export type RenderAccepted = Schemas['RenderAccepted']

export type Output = Schemas['OutputDetail']
export type EditTarget = Schemas['EditTarget']

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
export type PrintOptions = Schemas['PrintOptions']
export type PrintOptionsView = Schemas['PrintOptionsView']
export type PrintOptionsState = Schemas['PrintOptionsState']
export type PrintOptionsUpdate = Schemas['PrintOptionsUpdate']
/** Where an override is remembered. `request` is not one — it is not remembered. */
export type OptionScope = PrintOptionsUpdate['scope']

/** #87 — the filament picker's wire types. */
export type FilamentOptions = Schemas['FilamentOptions']
export type SpoolOption = Schemas['SpoolOption']
export type SlotNeed = Schemas['SlotNeed']
export type SlotChoice = Schemas['SlotChoice']
export type FilamentPlan = Schemas['FilamentPlan']
export type FilamentWarning = Schemas['FilamentWarning']
export type LoadedAt = Schemas['LoadedAt']

/**
 * #89 — run tracking.
 *
 * `PrintRoute` and `PrintStage` are named aliases onto the generated members rather
 * than string literals of their own: the backend's vocabulary is the contract, and a
 * hand-kept copy would drift the moment it gains a state.
 */
export type PrintProgress = Schemas['PrintProgress']
export type CopyProgress = Schemas['CopyProgress']
export type PrintRoute = PrintProgress['route']
export type PrintStage = PrintProgress['stage']

/**
 * #79 — projects. A project here is one of Bambuddy's plus the library folder that
 * belongs to it; ScadBuddy stores only which project a model's prints are filed under.
 */
export type ProjectView = Schemas['ProjectView']
export type ProjectChoices = Schemas['ProjectChoices']
export type ProjectRequest = Schemas['ProjectRequest']
export type ProjectAttach = Schemas['ProjectAttach']
export type AttachResult = Schemas['AttachResult']

/** #81 — the build volume the preview draws and checks the model against. */
export type Plate = Schemas['PlateView']
export type PlateCatalogue = Schemas['PlateCatalogue']
export type PlateFit = Schemas['PlateFit']

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
