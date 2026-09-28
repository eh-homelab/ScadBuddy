import { constants } from 'node:fs'
import { access, mkdir, readdir, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { claudeConfigDir, pluginCacheDir, scratchDir, type HarnessPaths } from './options.js'

// The deployment mounts an emptyDir (Kubernetes) or tmpfs (the CI smoke test)
// over the state directory so the root filesystem can stay read-only
// (README, "The agent sidecar"; spec §4.4). A mount REPLACES what the image
// created there at build time, so the Dockerfile's `install -d` of `claude/`
// and `work/` does not survive it. Recreate both at every start, idempotently,
// and fail before listening if that is impossible: a missing CLAUDE_CONFIG_DIR
// or cwd would otherwise surface only later, as an opaque spawn failure of the
// Claude Code subprocess on the first query.

export class StateDirError extends Error {
  override name = 'StateDirError'
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** A canonical 8-4-4-4-12 hex UUID: every session id (and so directory name) is one. */
export function isUuid(value: string): boolean {
  return UUID.test(value)
}

/**
 * A session's own working directory (#300, spec §6): `work/sessions/<id>`.
 * Deterministic from the session id, so every replica computes the same path
 * and a session's `cwd` is the same wherever it resumes (spec §3.1: the SDK's
 * store key "derives from the working directory"). The id must be a UUID, which
 * the session manager guarantees; anything else is refused so it can never
 * escape `work/`.
 */
export function sessionWorkDir(paths: HarnessPaths, sessionId: string): string {
  if (!isUuid(sessionId)) {
    throw new StateDirError(`not a session id: ${JSON.stringify(sessionId)}`)
  }
  return path.join(scratchDir(paths), 'sessions', sessionId.toLowerCase())
}

/**
 * A session's headless-browser directory (#349): `browser/<id>`, holding its
 * copy of the playwright plugin, the server's config and its output files.
 * Deliberately OUTSIDE the session's `cwd`: the server resolves a named output
 * file against its workspace root, which is that `cwd`, so nothing the model
 * names can overwrite the config it was started with.
 */
export function sessionBrowserDir(paths: HarnessPaths, sessionId: string): string {
  return path.join(paths.stateDir, 'browser', path.basename(sessionWorkDir(paths, sessionId)))
}

const BROWSER_TMP_PREFIX = 'sb-browser-'

/**
 * A session's headless-browser TMPDIR, where Chromium puts its profile:
 * `<os tmpdir>/sb-browser-<id>`. Short on purpose: Chromium's SingletonSocket
 * is a Unix socket under it, whose path is limited to ~107 bytes
 * (headlessBrowser.ts `serverCommand`).
 */
export function sessionBrowserTmpDir(sessionId: string): string {
  if (!isUuid(sessionId)) {
    throw new StateDirError(`not a session id: ${JSON.stringify(sessionId)}`)
  }
  return path.join(os.tmpdir(), `${BROWSER_TMP_PREFIX}${sessionId.toLowerCase()}`)
}

/**
 * Removes a session's headless-browser directories (review of #518): the
 * plugin copy, config and output files (screenshots) under `browser/<id>`, and
 * the TMPDIR holding Chromium's profile. Every turn writes them afresh, so the
 * session manager removes them when the turn ends; left alone they would fill
 * the volume.
 */
export async function removeSessionBrowserDirs(paths: HarnessPaths, sessionId: string): Promise<void> {
  await rm(sessionBrowserDir(paths, sessionId), { recursive: true, force: true, maxRetries: 3 })
  await rm(sessionBrowserTmpDir(sessionId), { recursive: true, force: true, maxRetries: 3 })
}

/**
 * Removes every session's headless-browser directories on this replica. Only at
 * start, before any turn runs here: what is left then is from a process that
 * died mid-turn and never reached removeSessionBrowserDirs.
 */
export async function sweepBrowserDirs(paths: HarnessPaths, tmp: string = os.tmpdir()): Promise<void> {
  await rm(path.join(paths.stateDir, 'browser'), { recursive: true, force: true })
  for (const name of await readdir(tmp).catch(() => [])) {
    if (name.startsWith(BROWSER_TMP_PREFIX) && isUuid(name.slice(BROWSER_TMP_PREFIX.length))) {
      await rm(path.join(tmp, name), { recursive: true, force: true })
    }
  }
}

/** Creates the session's working directory on this replica, idempotently. */
export async function ensureSessionDir(paths: HarnessPaths, sessionId: string): Promise<string> {
  const dir = sessionWorkDir(paths, sessionId)
  try {
    await mkdir(dir, { recursive: true })
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code ?? String(err)
    throw new StateDirError(`cannot create the session working directory ${dir} (${code})`)
  }
  return dir
}

export async function ensureStateDirs(paths: HarnessPaths): Promise<string[]> {
  // The plugin package cache (#297) is checked with the others, so an
  // unwritable one stops the pod here rather than failing the first install.
  const dirs = [claudeConfigDir(paths), scratchDir(paths), pluginCacheDir(paths)]
  for (const dir of dirs) {
    try {
      await mkdir(dir, { recursive: true })
      // mkdir succeeds on an existing directory even when it is read-only, so
      // check writability separately.
      await access(dir, constants.W_OK)
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code ?? String(err)
      throw new StateDirError(
        `cannot create a writable harness state directory ${dir} (${code}); ` +
          `mount a writable volume at ${paths.stateDir} owned by the service user`,
      )
    }
  }
  return dirs
}
