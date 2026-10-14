import { createHash } from 'node:crypto'
import { lstat, readdir, readFile } from 'node:fs/promises'
import path from 'node:path'

// The content hash of a plugin package: what the pin in `ai_plugin_packages`
// records next to the commit, and what a cached copy must match before the
// harness loads it (cache.ts).
//
// It is the SHA-256 of a canonical file list: one line per regular file,
// sorted by path, `<path>\0<x|->\0<sha256 of the bytes>\n`, where `x` marks
// an executable file. Directories are implied by their files (git has no
// empty directories either). Anything that is not a regular file or a
// directory (a symlink above all, which could point outside the package) is
// refused, as are packages too large to be skills and Markdown.

export const MAX_FILES = 2000
export const MAX_TOTAL_BYTES = 20 * 1024 * 1024
export const MAX_FILE_BYTES = 5 * 1024 * 1024

export type FileEntry = { sha256: string; size: number; executable: boolean }
export type FileList = Record<string, FileEntry>
export type TreeHash = { hash: string; files: FileList }

export class PackageContentError extends Error {
  override name = 'PackageContentError'
}

/** Paths are `/`-separated and relative to the package root. */
async function walk(root: string, rel: string, out: [string, string][]): Promise<void> {
  const dir = path.join(root, rel)
  const entries = await readdir(dir, { withFileTypes: true })
  for (const entry of entries) {
    const relPath = rel ? `${rel}/${entry.name}` : entry.name
    if (entry.isDirectory()) {
      await walk(root, relPath, out)
    } else if (entry.isFile()) {
      out.push([relPath, path.join(dir, entry.name)])
      if (out.length > MAX_FILES) throw new PackageContentError(`the package has more than ${MAX_FILES} files`)
    } else {
      throw new PackageContentError(
        `${relPath} is ${entry.isSymbolicLink() ? 'a symbolic link' : 'not a regular file'}; a package holds regular files only`,
      )
    }
  }
}

/** The canonical text the hash is taken over; exported for the tests. */
export function canonicalFileList(files: FileList): string {
  return Object.keys(files)
    .sort()
    .map((p) => `${p}\0${files[p]!.executable ? 'x' : '-'}\0${files[p]!.sha256}\n`)
    .join('')
}

export function hashOfFiles(files: FileList): string {
  return `sha256:${createHash('sha256').update(canonicalFileList(files)).digest('hex')}`
}

/** Hashes every file under `root`. Throws PackageContentError on a link, a special file, or a limit. */
export async function hashTree(root: string): Promise<TreeHash> {
  const found: [string, string][] = []
  await walk(root, '', found)
  const files: FileList = {}
  let total = 0
  for (const [rel, abs] of found.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    const stat = await lstat(abs)
    if (!stat.isFile()) throw new PackageContentError(`${rel} changed while it was read`)
    if (stat.size > MAX_FILE_BYTES) throw new PackageContentError(`${rel} is larger than ${MAX_FILE_BYTES} bytes`)
    total += stat.size
    if (total > MAX_TOTAL_BYTES) throw new PackageContentError(`the package is larger than ${MAX_TOTAL_BYTES} bytes`)
    const bytes = await readFile(abs)
    files[rel] = {
      sha256: createHash('sha256').update(bytes).digest('hex'),
      size: bytes.length,
      executable: (stat.mode & 0o111) !== 0,
    }
  }
  return { hash: hashOfFiles(files), files }
}

export type FileDiff = { added: string[]; removed: string[]; changed: string[] }

/** Which files a re-pin adds, removes and changes (content or executable bit). */
export function diffFiles(before: FileList, after: FileList): FileDiff {
  const added: string[] = []
  const removed: string[] = []
  const changed: string[] = []
  for (const p of Object.keys(after).sort()) {
    const old = before[p]
    if (!old) added.push(p)
    else if (old.sha256 !== after[p]!.sha256 || old.executable !== after[p]!.executable) changed.push(p)
  }
  for (const p of Object.keys(before).sort()) if (!after[p]) removed.push(p)
  return { added, removed, changed }
}
