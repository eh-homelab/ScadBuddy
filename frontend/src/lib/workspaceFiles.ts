import type { WorkspaceFile } from '../api/types'

// #2329 — the assistant panel's Files (components/assistant/SessionFiles.tsx): where an
// upload lands in the session's workspace, and how a file is named and described.

/** Where an upload lands. */
export const UPLOAD_DIR = 'uploads'

/** What a file's name becomes as one workspace path segment (agent `workspace/store.ts` WORKSPACE_PATH). */
export function segmentOf(name: string): string {
  const safe = name.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 120)
  return /^[A-Za-z0-9_]/.test(safe) ? safe : `_${safe}`.slice(0, 120)
}

/** `uploads/<name>`, numbered (`part-2.stl`) past the paths already taken. */
export function uploadPath(name: string, taken: ReadonlySet<string>): string {
  const segment = segmentOf(name)
  const dot = segment.lastIndexOf('.')
  const [stem, ext] = dot > 0 ? [segment.slice(0, dot), segment.slice(dot)] : [segment, '']
  let path = `${UPLOAD_DIR}/${segment}`
  for (let n = 2; taken.has(path); n++) path = `${UPLOAD_DIR}/${stem}-${n}${ext}`
  return path
}

/** What the composer gets for a file, so the next message names it to the assistant. */
export const fileReference = (path: string) => `[workspace file ${path}]`

/** Where a file came from, in a few words. */
export function sourceLabel(source: WorkspaceFile['source']): string {
  if (!source) return ''
  if (source.kind === 'upload') return 'uploaded by you'
  if (typeof source.url === 'string') {
    try {
      return `from ${new URL(source.url).host}`
    } catch {
      return 'from the web'
    }
  }
  if (typeof source.tool === 'string') return `by ${source.tool}`
  return ''
}
