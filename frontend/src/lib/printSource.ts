import { api } from '../api/client'
import type { Within } from './traceAction'
import type {
  AttachResult,
  ChoicesView,
  FilamentOptions,
  LibraryEntry,
  ModelPrintChoices,
  Output,
  OutputPlate,
  PrintCheck,
  PreviewStarted,
  SlicePreview,
  PrintProgress,
  PrintRunRequest,
  PrintRunResult,
  ProjectAttach,
} from '../api/types'

/**
 * #313 — what the Print dialog prints: an output ScadBuddy rendered, or a file already
 * in Bambuddy's library. Everything else about the dialog is the same.
 */
export type PrintSource =
  | {
      kind: 'output'
      /** `manifest`, `colors` and `name` are what Re-arrange needs, when the caller has them (§7). */
      output: Pick<Output, 'id' | 'slug'> & Partial<Pick<Output, 'manifest' | 'colors' | 'name'>>
    }
  | { kind: 'library'; file: Pick<LibraryEntry, 'id' | 'filename'> }

export type FilamentQuery = { printerId?: number | null; plateId?: number; allPlates?: boolean }

/** The dialog's reads and writes for one source. */
export interface SourceApi {
  getChoices: (printerId: number | null) => Promise<ChoicesView>
  getFilaments: (query: FilamentQuery) => Promise<FilamentOptions>
  getPlates: () => Promise<OutputPlate[]>
  plateThumbnailUrl: (index: number) => string
  /** #1723 — the mesh the 3D preview shows: an output's whole, a library file's plate. */
  previewUrl: (plate: number) => string
  /**
   * `signal` stops waiting on the run (the dialog went away); the run itself goes on.
   * `within` keeps the POST, retries included, in a traced action.
   */
  run: (body: PrintRunRequest, signal?: AbortSignal, within?: Within) => Promise<PrintRunResult>
  /** #755, #760 — what the run would refuse for `body`, with nothing uploaded or queued. */
  check: (body: PrintRunRequest) => Promise<PrintCheck>
  /** #2169 — slice `body` in the background through the run's own path; nothing queued. */
  preview: (body: PrintRunRequest) => Promise<PreviewStarted>
  /** #2169 — how one of this source's background slices stands. */
  readPreview: (jobId: number) => Promise<SlicePreview>
  /** What this source reopens on next time: per model for an output, per file here. */
  remember: (choices: ModelPrintChoices) => Promise<ModelPrintChoices>
}

/** One string per source, so what belongs to one is reset when it changes. */
export function sourceKey(source: PrintSource | undefined): string | undefined {
  if (!source) return undefined
  return source.kind === 'output' ? `output:${source.output.id}` : `library:${source.file.id}`
}

/**
 * #1754 — what a print's own options and remembered choices are kept under, in the one
 * store (`backend/scadbuddy/bambuddy/options.py` `options_scope`): an output's model, which
 * its every output shares, or a library file's `library:<file id>`. The `model` scope of
 * `PUT /settings/print-options` takes either as its key.
 */
export interface OptionsSubject {
  key: string
  /** What "This …" names in the dialog. */
  noun: 'model' | 'file'
}

const LIBRARY_SCOPE = /^library:(\d+)$/

export function libraryOptionsScope(fileId: number): string {
  return `library:${fileId}`
}

/** The library file a scope key names, or null for a model's. */
export function libraryFileOfScope(key: string): number | null {
  const match = LIBRARY_SCOPE.exec(key)
  return match ? Number(match[1]) : null
}

export function optionsSubject(source: PrintSource | undefined): OptionsSubject | undefined {
  if (!source) return undefined
  return source.kind === 'output'
    ? { key: source.output.slug, noun: 'model' }
    : { key: libraryOptionsScope(source.file.id), noun: 'file' }
}

/**
 * #1751 — what a print is followed by: an output's id, or a library file's
 * `library:<file id>`. It is the print's run subject, and `print:<it>` is its realtime topic.
 */
export function printSubject(source: PrintSource | undefined): string | undefined {
  if (!source) return undefined
  return source.kind === 'output' ? source.output.id : libraryOptionsScope(source.file.id)
}

/** The progress of a `printSubject`'s print, from the route for its kind. */
export function readPrintProgress(subject: string): Promise<PrintProgress | null> {
  const fileId = libraryFileOfScope(subject)
  return fileId === null ? api.getPrintProgress(subject) : api.getLibraryPrintProgress(fileId)
}

/** #79, #1751 — file a `printSubject`'s queue entries, and their archives, under a project. */
export function attachPrintToProject(subject: string, body: ProjectAttach): Promise<AttachResult> {
  const fileId = libraryFileOfScope(subject)
  return fileId === null ? api.attachToProject(subject, body) : api.attachLibraryToProject(fileId, body)
}

/** Looked up at call time, so a test's `vi.spyOn(api, …)` still sees every call. */
export function sourceApi(source: PrintSource): SourceApi {
  if (source.kind === 'output') {
    const { id, slug } = source.output
    return {
      getChoices: (printerId) => api.getChoices(id, printerId),
      getFilaments: (query) => api.getFilaments(id, query),
      getPlates: () => api.getOutputPlates(id),
      plateThumbnailUrl: (index) => api.outputPlateThumbnailUrl(id, index),
      previewUrl: () => api.outputPreviewGlbUrl(id),
      run: (body, signal, within) => api.runPrint(id, body, signal, within),
      check: (body) => api.checkPrint(id, body),
      preview: (body) => api.previewSlice(id, body),
      readPreview: (jobId) => api.getPreviewSlice(id, jobId),
      remember: (choices) => api.putModelChoices(slug, choices),
    }
  }
  const { id } = source.file
  return {
    getChoices: (printerId) => api.getLibraryChoices(id, printerId),
    getFilaments: (query) => api.getLibraryFilaments(id, query),
    getPlates: () => api.getLibraryPlates(id),
    plateThumbnailUrl: (index) => api.libraryPlateThumbnailUrl(id, index),
    previewUrl: (plate) => api.libraryPreviewGlbUrl(id, plate),
    run: (body, signal, within) => api.runLibraryPrint(id, body, signal, within),
    check: (body) => api.checkLibraryPrint(id, body),
    preview: (body) => api.previewLibrarySlice(id, body),
    readPreview: (jobId) => api.getLibraryPreviewSlice(id, jobId),
    remember: (choices) => api.putLibraryChoices(id, choices),
  }
}
