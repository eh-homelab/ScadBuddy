import { readdir, readFile, rm, stat } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { type SessionFiles, WorkspaceError, type WorkspaceRef } from '../workspace/store.js'
import type { HookCallbackMatcher } from '@anthropic-ai/claude-agent-sdk'
import { PLAIN_FILE_NAME, TOOL_PREFIX } from './headlessBrowser.js'

// The headless browser's downloads, into the session's workspace (#2329 part 2).
//
// The server's context accepts downloads (headlessBrowser.ts `playwrightConfig`),
// and the request guard (`redirectGuardSource`) saves each one in the session's
// downloads directory as `<uuid>.bin`, with a `<uuid>.pending` marker while it
// runs and a `<uuid>.json` (its URL, its suggested name, or the error) once it
// has ended. After every browser tool call the harness's PostToolUse hook
// (run.ts) drains that directory with `drainDownloads`: each ended download
// becomes the workspace file `downloads/<safe name>`, source
// `{ tool: 'browser', url }` with the URL's query and fragment dropped (they can
// carry a site's token), under the workspace's caps, and both files are removed
// from the disk whatever happened. The name follows the input guard's rule for a
// `filename` (headlessBrowser.ts PLAIN_FILE_NAME); a name already taken gets a
// `-1`, `-2` … before its extension, so a download never replaces a file.
//
// The model is told what landed, as the hook's `additionalContext`, by path,
// size and sha256 (never the URL: workspace_list shows the source).

/** Where downloads land in the workspace. */
export const DOWNLOADS_PREFIX = 'downloads'

/** How long the hook waits for a download the call started to finish. */
export const DOWNLOAD_WAIT_MS = 10_000

const ENTRY = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(bin|json|pending)$/

export type DownloadTarget = { files: Pick<SessionFiles, 'write' | 'limits'>; ref: WorkspaceRef }

export type DrainedFile = { path: string; size_bytes: number; sha256: string; media_type: string }
export type Drained = {
  saved: DrainedFile[]
  /** Downloads that were not stored, and why; their bytes are gone from the disk too. */
  refused: { name: string; reason: string }[]
  /** Downloads still running when the drain gave up waiting; a later call drains them. */
  running: number
}

/** A suggested file name made a plain one: other characters become `-`, no leading dot, no `..`, at most 128 characters. */
export function downloadName(suggested: string): string {
  const base = suggested.split(/[\\/]/).at(-1) ?? ''
  let clean = base
    .replace(/[^A-Za-z0-9_.-]+/g, '-')
    .replace(/\.{2,}/g, '.')
    .replace(/^[^A-Za-z0-9_]+/, '')
  if (clean.length > 128) {
    const ext = /\.[A-Za-z0-9]{1,16}$/.exec(clean)?.[0] ?? ''
    clean = clean.slice(0, 128 - ext.length) + ext
  }
  return PLAIN_FILE_NAME.test(clean) ? clean : 'download'
}

/** The URL a download came from, without its query and fragment; the scheme alone for anything not http(s) or blob. */
export function downloadSource(url: string): string {
  try {
    const u = new URL(url)
    if (u.protocol === 'http:' || u.protocol === 'https:') return `${u.origin}${u.pathname}`
    if (u.protocol === 'blob:') return `blob:${u.origin}`
    return u.protocol
  } catch {
    return 'unknown'
  }
}

/** `name` with `-n` before its extension. */
function numbered(name: string, n: number): string {
  if (n === 0) return name
  const dot = name.lastIndexOf('.')
  const [stem, ext] = dot > 0 ? [name.slice(0, dot), name.slice(dot)] : [name, '']
  return `${stem.slice(0, 128 - ext.length - String(n).length - 1)}-${n}${ext}`
}

type Meta = { url?: unknown; name?: unknown; error?: unknown }

async function entries(dir: string): Promise<{ ended: string[]; running: number }> {
  let names: string[]
  try {
    names = await readdir(dir)
  } catch {
    return { ended: [], running: 0 }
  }
  const ended: string[] = []
  let running = 0
  for (const name of names) {
    const m = ENTRY.exec(name)
    if (!m) continue
    if (m[2] === 'json') ended.push(m[1]!)
    if (m[2] === 'pending') running++
  }
  return { ended: ended.sort(), running }
}

/**
 * Moves every ended download in `dir` into the workspace and off the disk.
 * With `waitMs`, first waits up to that long for running ones to end. Never
 * throws for one download: a refusal (caps, an unreadable file) is reported in
 * `refused`.
 */
export async function drainDownloads(
  dir: string,
  target: DownloadTarget,
  options: { waitMs?: number; pollMs?: number } = {},
): Promise<Drained> {
  const deadline = performance.now() + (options.waitMs ?? 0)
  let found = await entries(dir)
  while (found.running > 0 && performance.now() < deadline) {
    await sleep(options.pollMs ?? 100)
    found = await entries(dir)
  }
  const drained: Drained = { saved: [], refused: [], running: found.running }
  if (found.ended.length === 0) return drained
  const { fileMaxBytes } = await target.files.limits()
  for (const id of found.ended) {
    const metaFile = path.join(dir, `${id}.json`)
    const binFile = path.join(dir, `${id}.bin`)
    let name = 'download'
    try {
      const meta = JSON.parse(await readFile(metaFile, 'utf8')) as Meta
      name = downloadName(typeof meta.name === 'string' ? meta.name : '')
      if (typeof meta.error === 'string' && meta.error) {
        drained.refused.push({ name, reason: 'the download failed in the browser' })
        continue
      }
      const size = (await stat(binFile)).size
      if (size > fileMaxBytes) {
        drained.refused.push({ name, reason: `${size} bytes, over the ${fileMaxBytes}-byte workspace file limit` })
        continue
      }
      const bytes = await readFile(binFile)
      const source = { tool: 'browser', url: downloadSource(typeof meta.url === 'string' ? meta.url : '') }
      drained.saved.push(await store(target, name, bytes, source))
    } catch (err) {
      drained.refused.push({ name, reason: err instanceof WorkspaceError ? err.message : 'the download could not be read' })
    } finally {
      await rm(binFile, { force: true })
      await rm(metaFile, { force: true })
    }
  }
  return drained
}

/** Stores the bytes under `downloads/<name>`, or the first numbered name not yet taken. */
async function store(target: DownloadTarget, name: string, bytes: Buffer, source: { tool: string; url: string }): Promise<DrainedFile> {
  for (let n = 0; ; n++) {
    try {
      const info = await target.files.write(target.ref, `${DOWNLOADS_PREFIX}/${numbered(name, n)}`, bytes, { source, expectedSha256: null })
      return { path: info.path, size_bytes: info.size, sha256: info.sha256, media_type: info.mediaType }
    } catch (err) {
      if (err instanceof WorkspaceError && err.code === 'conflict' && n < 100) continue
      throw err
    }
  }
}

/**
 * The PostToolUse hook that drains `dir` after every headless-browser tool
 * call, waiting up to `waitMs` for a download the call started, and tells the
 * model what landed. Never fails the call: an error drains nothing.
 */
export function downloadsHook(
  dir: string,
  target: DownloadTarget,
  options: { waitMs?: number; onError?: (err: unknown) => void } = {},
): HookCallbackMatcher {
  return {
    // Claude Code matches hook matchers as regular expressions; the prefix has no special characters.
    matcher: `^${TOOL_PREFIX}`,
    hooks: [
      async (input) => {
        if (input.hook_event_name !== 'PostToolUse' || !input.tool_name.startsWith(TOOL_PREFIX)) return {}
        try {
          const context = describeDrained(await drainDownloads(dir, target, { waitMs: options.waitMs ?? DOWNLOAD_WAIT_MS }))
          return context ? { hookSpecificOutput: { hookEventName: 'PostToolUse', additionalContext: context } } : {}
        } catch (err) {
          options.onError?.(err)
          return {}
        }
      },
    ],
  }
}

/** What the model is told after a call that downloaded something; undefined when nothing happened. */
export function describeDrained(drained: Drained): string | undefined {
  const lines: string[] = []
  for (const f of drained.saved) {
    lines.push(`- ${f.path} (${f.media_type}, ${f.size_bytes} bytes, sha256 ${f.sha256})`)
  }
  for (const r of drained.refused) lines.push(`- ${r.name}: not saved, ${r.reason}`)
  if (drained.running > 0) lines.push(`- ${drained.running} still downloading; it lands after a later browser call`)
  if (lines.length === 0) return undefined
  return (
    'Headless-browser downloads, now in your workspace (workspace_read reads them; an upload tool sends one ' +
    `with workspace_path and workspace_sha256):\n${lines.join('\n')}`
  )
}
