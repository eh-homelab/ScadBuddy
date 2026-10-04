import { api } from '../api/client'
import type { Within } from './traceAction'
import type {
  ChoicesView,
  FilamentOptions,
  LibraryEntry,
  ModelPrintChoices,
  Output,
  OutputPlate,
  PrintCheck,
  PrintRunRequest,
  PrintRunResult,
} from '../api/types'

/**
 * #313 — what the Print dialog prints: an output ScadBuddy rendered, or a file already
 * in Bambuddy's library. Everything else about the dialog is the same.
 */
export type PrintSource =
  | { kind: 'output'; output: Pick<Output, 'id' | 'slug'> }
  | { kind: 'library'; file: Pick<LibraryEntry, 'id' | 'filename'> }

export type FilamentQuery = { printerId?: number | null; plateId?: number; allPlates?: boolean }

/** The dialog's reads and writes for one source. */
export interface SourceApi {
  getChoices: (printerId: number | null) => Promise<ChoicesView>
  getFilaments: (query: FilamentQuery) => Promise<FilamentOptions>
  getPlates: () => Promise<OutputPlate[]>
  plateThumbnailUrl: (index: number) => string
  /**
   * `signal` stops waiting on the run (the dialog went away); the run itself goes on.
   * `within` keeps the POST, retries included, in a traced action.
   */
  run: (body: PrintRunRequest, signal?: AbortSignal, within?: Within) => Promise<PrintRunResult>
  /** #755, #760 — what the run would refuse for `body`, with nothing uploaded or queued. */
  check: (body: PrintRunRequest) => Promise<PrintCheck>
  /** What this source reopens on next time: per model for an output, per file here. */
  remember: (choices: ModelPrintChoices) => Promise<ModelPrintChoices>
}

/** One string per source, so what belongs to one is reset when it changes. */
export function sourceKey(source: PrintSource | undefined): string | undefined {
  if (!source) return undefined
  return source.kind === 'output' ? `output:${source.output.id}` : `library:${source.file.id}`
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
      run: (body, signal, within) => api.runPrint(id, body, signal, within),
      check: (body) => api.checkPrint(id, body),
      remember: (choices) => api.putModelChoices(slug, choices),
    }
  }
  const { id } = source.file
  return {
    getChoices: (printerId) => api.getLibraryChoices(id, printerId),
    getFilaments: (query) => api.getLibraryFilaments(id, query),
    getPlates: () => api.getLibraryPlates(id),
    plateThumbnailUrl: (index) => api.libraryPlateThumbnailUrl(id, index),
    run: (body, signal, within) => api.runLibraryPrint(id, body, signal, within),
    check: (body) => api.checkLibraryPrint(id, body),
    remember: (choices) => api.putLibraryChoices(id, choices),
  }
}
