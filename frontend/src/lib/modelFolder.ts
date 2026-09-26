/**
 * #179 — reading a dropped model directory, the layout of `models/<slug>/`:
 * `model.scad`, `model.json`, `thumbnail.png` and `README.md`.
 *
 * The server takes a multipart upload's slug from the source's filename, the way the
 * image seed takes it from the directory's name. So a directory's source is sent
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

export interface Classified {
  files: ModelFiles | null
  /** Names that were not used: a second source, an unrelated file. */
  ignored: string[]
}

const lower = (file: File) => file.name.toLowerCase()

export const isScad = (file: File) => lower(file).endsWith('.scad')
export const isPng = (file: File) => lower(file).endsWith('.png') || file.type === 'image/png'
export const isMarkdown = (file: File) => /\.(md|markdown)$/.test(lower(file))

/** The preferred file among candidates: the one with the bundled layout's name, else the first. */
function pick(candidates: File[], preferred: string): File | undefined {
  return candidates.find((file) => lower(file) === preferred) ?? candidates[0]
}

/** Sort a set of chosen or dropped files into the parts of a model upload. */
export function classifyFiles(chosen: File[], folder?: string): Classified {
  const scad = pick(chosen.filter(isScad), 'model.scad')
  if (!scad) return { files: null, ignored: chosen.map((file) => file.name) }
  const meta = pick(
    chosen.filter((file) => lower(file) === 'model.json'),
    'model.json',
  )
  const thumbnail = pick(chosen.filter(isPng), 'thumbnail.png')
  const readme = pick(chosen.filter(isMarkdown), 'readme.md')
  const used = new Set([scad, meta, thumbnail, readme])
  const ignored = chosen.filter((file) => !used.has(file)).map((file) => file.name)
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
