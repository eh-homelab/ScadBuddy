import { constants, renameSync, writeFileSync } from 'node:fs'
import { lstat, open, readdir, realpath, unlink } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { type SessionFiles, WorkspaceError, type WorkspaceRef } from '../workspace/store.js'
import type { HookCallbackMatcher } from '@anthropic-ai/claude-agent-sdk'
import { type DownloadBudget, NO_DOWNLOADS, PLAIN_FILE_NAME, TOOL_PREFIX } from './headlessBrowser.js'

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
//
// The caps hold WHILE a download happens, not only when it is drained, so no
// download can fill the pod's disk: before every browser call a PreToolUse hook
// (`downloadBudgetHook`) writes the call's budget (the per-file cap, the room
// left in the workspace, how many downloads a call and a turn may make), and
// the request guard holds every download to it. A navigation the browser would
// download is refused before it reaches the disk when its Content-Length, or
// the body actually read, is over the cap; a download that starts anyway (one
// made by page script, say) is watched on the disk, cancelled and deleted once
// it passes the budget; and one past the count is cancelled at once. Each
// refusal is reported here like a finished download, with its reason.

/** Where downloads land in the workspace. */
export const DOWNLOADS_PREFIX = 'downloads'

/** How long the hook waits for a download the call started to finish. */
export const DOWNLOAD_WAIT_MS = 10_000

const ENTRY = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(bin|json|pending)$/

export type DownloadTarget = { files: Pick<SessionFiles, 'write' | 'limits' | 'usage'>; ref: WorkspaceRef }

/** How many downloads one browser call, and one turn, may make. */
export const MAX_DOWNLOADS_PER_CALL = 5
export const MAX_DOWNLOADS_PER_TURN = 20

/** Writes the budget the request guard reads, whole (a rename), so it never reads half of one. */
export function writeDownloadBudget(file: string, budget: DownloadBudget): void {
  const tmp = `${file}.tmp`
  writeFileSync(tmp, JSON.stringify(budget))
  renameSync(tmp, file)
}

/**
 * The PreToolUse hook that, before every headless-browser call, writes what
 * the call may download: the workspace's per-file cap and the room left in it
 * (its quota less what it holds), at most MAX_DOWNLOADS_PER_CALL downloads,
 * and for the turn at most MAX_DOWNLOADS_PER_TURN and the room there was at its
 * first call. The request guard holds every download to it while it runs
 * (headlessBrowser.ts `redirectGuardSource`). A budget that cannot be read
 * from the workspace is written as none: nothing downloads.
 */
export function downloadBudgetHook(budgetFile: string, target: DownloadTarget, options: { onError?: (err: unknown) => void } = {}): HookCallbackMatcher {
  let call = 0
  let turnMaxBytes: number | undefined
  return {
    matcher: `^${TOOL_PREFIX}`,
    hooks: [
      async (input) => {
        if (input.hook_event_name !== 'PreToolUse' || !input.tool_name.startsWith(TOOL_PREFIX)) return {}
        call++
        try {
          const [limits, usage] = await Promise.all([target.files.limits(), target.files.usage(target.ref)])
          const room = Math.max(0, limits.workspaceMaxBytes - usage.bytes)
          turnMaxBytes ??= room
          writeDownloadBudget(budgetFile, {
            call,
            fileMaxBytes: Math.min(limits.fileMaxBytes, room),
            callMaxBytes: room,
            callMaxCount: MAX_DOWNLOADS_PER_CALL,
            turnMaxBytes,
            turnMaxCount: MAX_DOWNLOADS_PER_TURN,
          })
        } catch (err) {
          options.onError?.(err)
          try {
            writeDownloadBudget(budgetFile, { ...NO_DOWNLOADS, call })
          } catch (inner) {
            // The file materialize wrote, NO_DOWNLOADS, or an older budget stays.
            options.onError?.(inner)
          }
        }
        return {}
      },
    ],
  }
}

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

/** A meta file is a URL, a name and a reason; anything larger is not the guard's. */
const META_MAX_BYTES = 1024 * 1024

type Meta = { url?: unknown; name?: unknown; error?: unknown; refused?: unknown }

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
  const own = await realpath(dir)
  for (const id of found.ended) {
    // Built here from the directory's real path and the entry's uuid, never from the meta.
    const metaFile = path.join(own, `${id}.json`)
    const binFile = path.join(own, `${id}.bin`)
    let name = 'download'
    try {
      const raw = await readOwn(metaFile, META_MAX_BYTES)
      if (typeof raw === 'number') throw new Error('meta too large')
      const meta = JSON.parse(raw.toString('utf8')) as Meta
      name = downloadName(typeof meta.name === 'string' ? meta.name : '')
      if (typeof meta.refused === 'string' && meta.refused) {
        drained.refused.push({ name, reason: `stopped by the download limits: ${meta.refused}` })
        continue
      }
      if (typeof meta.error === 'string' && meta.error) {
        drained.refused.push({ name, reason: 'the download failed in the browser' })
        continue
      }
      const bytes = await readOwn(binFile, fileMaxBytes)
      if (typeof bytes === 'number') {
        drained.refused.push({ name, reason: `${bytes} bytes, over the ${fileMaxBytes}-byte workspace file limit` })
        continue
      }
      const source = { tool: 'browser', url: downloadSource(typeof meta.url === 'string' ? meta.url : '') }
      drained.saved.push(await store(target, name, bytes, source))
    } catch (err) {
      drained.refused.push({ name, reason: err instanceof WorkspaceError ? err.message : 'the download could not be read' })
    } finally {
      await removeOwn(binFile)
      await removeOwn(metaFile)
    }
  }
  return drained
}

/**
 * A plain file's bytes, opened with O_NOFOLLOW so a symlink planted in the
 * downloads directory is refused, never followed (ELOOP); its size instead
 * when it is larger than `maxBytes`.
 */
async function readOwn(file: string, maxBytes: number): Promise<Buffer | number> {
  const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW)
  try {
    const st = await handle.stat()
    if (!st.isFile()) throw new Error('not a plain file')
    if (st.size > maxBytes) return st.size
    return await handle.readFile()
  } finally {
    await handle.close()
  }
}

/** Removes a file, or the link itself for a symlink (never its target); anything else is left. */
async function removeOwn(file: string): Promise<void> {
  try {
    const st = await lstat(file)
    if (st.isFile() || st.isSymbolicLink()) await unlink(file)
  } catch {
    // Already gone.
  }
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
