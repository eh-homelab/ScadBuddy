/**
 * Wire types for the ScadBuddy API (spec §5.1, §8).
 * Hand-written against the documented contract; the backend is the source of truth.
 */

export type ParamType =
  | 'number'
  | 'integer'
  | 'string'
  | 'boolean'
  | 'select'
  | 'color'
  | 'font'
  | 'slider'

export type ParamValue = string | number | boolean

export interface ParamOption {
  name: string
  value: ParamValue
}

export interface Param {
  name: string
  type: ParamType
  initial: ParamValue
  caption?: string
  min?: number
  max?: number
  step?: number
  maxLength?: number
  options?: ParamOption[]
}

export interface ParamGroup {
  name: string
  params: Param[]
}

export interface ModelSchema {
  title: string
  groups: ParamGroup[]
}

export interface ModelSummary {
  slug: string
  name: string
  description?: string
  tags: string[]
  thumbnail_url?: string
  updated_at: string
  last_generated_at?: string
  output_count: number
}

export type JobStatus = 'pending' | 'running' | 'done' | 'failed'

export interface Bbox {
  x: number
  y: number
  z: number
}

export interface Job {
  job_id: string
  slug: string
  status: JobStatus
  log_tail?: string
  preview_url?: string
  bbox_mm?: Bbox
  colors?: string[]
  created_at: string
}

export interface Output {
  id: string
  slug: string
  created_at: string
  params: Record<string, ParamValue>
  bbox_mm?: Bbox
  colors: string[]
  thumbnail_url?: string
  library_file_id?: string
  queue_item_id?: string
}

export interface FontFamily {
  family: string
  styles: string[]
}

export interface Settings {
  bambuddy_url: string
  /** Write-only: the API never returns the key, only whether one is stored. */
  api_key_set: boolean
  library_folder_id?: string
  pipeline_id?: string
  sidebar_registered: boolean
}

export interface SettingsUpdate {
  bambuddy_url: string
  /** Omit to leave the stored key untouched; empty string clears it. */
  api_key?: string
  library_folder_id?: string
  pipeline_id?: string
}

export interface ConnectionTest {
  ok: boolean
  detail: string
  printers?: { id: string; name: string }[]
}

export interface BambuddyFolder {
  id: string
  name: string
}

export interface BambuddyPipeline {
  id: string
  name: string
}

export interface BambuddyTargets {
  folders: BambuddyFolder[]
  pipelines: BambuddyPipeline[]
}

export type SendMode = 'library' | 'queue'

export interface SendRequest {
  mode: SendMode
  copies: number
}

export interface SendResult {
  mode: SendMode
  library_file_id: string
  queue_item_id?: string
  queue_url?: string
}

/** RFC 9457 problem details. */
export interface Problem {
  type?: string
  title: string
  status: number
  detail?: string
}
