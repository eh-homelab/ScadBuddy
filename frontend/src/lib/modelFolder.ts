/**
 * #179 — reading a dropped model directory, the layout of `models/<slug>/`:
 * `model.scad`, `model.json`, `thumbnail.png` and `README.md`.
 *
 * The server takes a multipart upload's slug from the source's filename, the way the
 * built-in sync takes a bundled model's from its directory's name. So a directory's source is sent
 * named after the directory — `model.scad` as it is would make every one `model`.
 */

export interface ModelFiles {
  scad: File
  meta?: File
  thumbnail?: File
  readme?: File
  /** The directory the files came from, when they came from one. */
  folder?: string
}

/** A chosen file that is not uploaded, and why when another file took its place. */
export interface Skipped {
  name: string
  /** Set when it lost to another file of its kind, e.g. `thumbnail is cover.png`. */
  reason?: string
}

export interface Classified {
  files: ModelFiles | null
  /** Files that were not used: a second source, image or README, an unrelated file. */
  ignored: Skipped[]
}

const lower = (file: File) => file.name.toLowerCase()

export const isScad = (file: File) => lower(file).endsWith('.scad')
export const isPng = (file: File) => lower(file).endsWith('.png') || file.type === 'image/png'
export const isMarkdown = (file: File) => /\.(md|markdown)$/.test(lower(file))

/** The server's `MAX_THUMBNAIL_BYTES`: each thumbnail set is kept in the history. */
export const MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
/** The limit as people read it, derived so the message cannot drift from it. */
export const MAX_THUMBNAIL_SIZE = `${MAX_THUMBNAIL_BYTES / (1024 * 1024)} MiB`

/** Why a file cannot be a model's thumbnail, or null when it can. */
export function thumbnailProblem(file: File): string | null {
  if (!isPng(file)) return 'The thumbnail must be a PNG.'
  if (file.size > MAX_THUMBNAIL_BYTES) {
    return `The thumbnail must be ${MAX_THUMBNAIL_SIZE} or smaller.`
  }
  return null
}

/** The server's `MAX_SOURCE_CHARS`, which caps a README on every write path. */
export const MAX_README_CHARS = 1_000_000

/**
 * Why text cannot be a model's README, or null when it can. Counted in code points,
 * as the server counts a Python `str` -- not UTF-16 units, which count an emoji
 * twice. `length` is never below the code-point count, so text within it is fine
 * without counting.
 */
export function readmeProblem(text: string): string | null {
  if (text.length <= MAX_README_CHARS || [...text].length <= MAX_README_CHARS) return null
  return `The README must be at most ${MAX_README_CHARS.toLocaleString('en')} characters.`
}

/** Code-unit order: the same on every browser, OS and locale, unlike `localeCompare`. */
function byName(a: File, b: File): number {
  const [x, y] = [a.name, b.name]
  if (x !== y) return x < y ? -1 : 1
  const [p, q] = [a.webkitRelativePath, b.webkitRelativePath]
  return p < q ? -1 : p > q ? 1 : 0
}

/**
 * The preferred file among candidates: the one with the bundled layout's name, else
 * the first by name. Never the first as listed: a directory's entries come back in
 * whatever order the browser and file system give, so that would pick differently
 * for the same folder.
 */
function pick(candidates: File[], preferred: string): File | undefined {
  const sorted = [...candidates].sort(byName)
  return sorted.find((file) => lower(file) === preferred) ?? sorted[0]
}

/** Sort a set of chosen or dropped files into the parts of a model upload. */
export function classifyFiles(chosen: File[], folder?: string): Classified {
  const scad = pick(chosen.filter(isScad), 'model.scad')
  if (!scad) return { files: null, ignored: [...chosen].sort(byName).map(({ name }) => ({ name })) }
  const meta = pick(
    chosen.filter((file) => lower(file) === 'model.json'),
    'model.json',
  )
  const thumbnail = pick(chosen.filter(isPng), 'thumbnail.png')
  const readme = pick(chosen.filter(isMarkdown), 'readme.md')
  const used = new Set([scad, meta, thumbnail, readme])
  // Two files can share a name from different folders of one drop: the winner is
  // then named by its path, so the reason still says which one was used.
  const named = (winner: File, loser: File) =>
    winner.name === loser.name && winner.webkitRelativePath
      ? winner.webkitRelativePath
      : winner.name
  const reason = (file: File): string | undefined => {
    if (meta && lower(file) === 'model.json') return `metadata is ${named(meta, file)}`
    if (isScad(file)) return `source is ${named(scad, file)}`
    if (thumbnail && isPng(file)) return `thumbnail is ${named(thumbnail, file)}`
    if (readme && isMarkdown(file)) return `README is ${named(readme, file)}`
    return undefined
  }
  const ignored = [...chosen]
    .sort(byName)
    .filter((file) => !used.has(file))
    .map((file) => {
      const why = reason(file)
      return why ? { name: file.name, reason: why } : { name: file.name }
    })
  return { files: { scad, meta, thumbnail, readme, folder }, ignored }
}

/** The top directory of files chosen through a directory picker (`webkitRelativePath`). */
export function folderOf(chosen: File[]): string | undefined {
  const path = chosen[0]?.webkitRelativePath
  if (!path || !path.includes('/')) return undefined
  return path.split('/')[0] || undefined
}

/**
 * What the source is sent as, which decides the slug: the directory's name when the
 * files came from one; the `model.json` name when a bare `model.scad` was dropped
 * beside it; otherwise the file's own name.
 */
export function uploadFilename(files: ModelFiles, metaName?: string): string {
  if (files.folder) return `${files.folder}.scad`
  if (lower(files.scad) === 'model.scad' && metaName?.trim()) return `${metaName.trim()}.scad`
  return files.scad.name
}

/** The `name` in a `model.json`, or undefined when it is missing or unreadable. */
export async function readMetaName(meta: File | undefined): Promise<string | undefined> {
  if (!meta) return undefined
  try {
    const parsed: unknown = JSON.parse(await meta.text())
    if (parsed && typeof parsed === 'object' && 'name' in parsed) {
      const { name } = parsed as { name: unknown }
      return typeof name === 'string' ? name : undefined
    }
  } catch {
    // The server reports a model.json it cannot read; here it only names the upload.
  }
  return undefined
}

function readEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject))
}

function fileOf(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject))
}

/**
 * The files of a drop. A single dropped directory is read one level deep, which is
 * the whole of a bundled model; anything else is the drop's own file list.
 */
export async function droppedFiles(transfer: DataTransfer): Promise<{
  files: File[]
  folder?: string
}> {
  const items = Array.from(transfer.items ?? [])
  const entries = items
    .map((item) => (typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null))
    .filter((entry): entry is FileSystemEntry => entry !== null)
  const [only] = entries
  if (entries.length === 1 && only?.isDirectory) {
    const reader = (only as FileSystemDirectoryEntry).createReader()
    const found: FileSystemEntry[] = []
    // `readEntries` answers in batches and signals the end with an empty one.
    for (let batch = await readEntries(reader); batch.length > 0; batch = await readEntries(reader)) {
      found.push(...batch)
    }
    const files = await Promise.all(
      found.filter((entry) => entry.isFile).map((entry) => fileOf(entry as FileSystemFileEntry)),
    )
    return { files, folder: only.name }
  }
  return { files: Array.from(transfer.files) }
}
