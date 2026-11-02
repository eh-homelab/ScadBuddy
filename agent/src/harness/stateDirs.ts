import { constants } from 'node:fs'
import { access, chmod, copyFile, lstat, mkdir, readdir, readlink, rm, symlink } from 'node:fs/promises'
import path from 'node:path'
import { isUuid } from './uuid.js'
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

export { isUuid }

/** Where the sessions' working directories are: SCADBUDDY_SESSION_DIR's, else `work/sessions` under the state directory. */
export function sessionsRoot(paths: HarnessPaths): string {
  return paths.sessionsDir ?? path.join(scratchDir(paths), 'sessions')
}

/**
 * A session's own working directory (#300, spec §6): `<sessionsRoot>/<id>`.
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
  return path.join(sessionsRoot(paths), sessionId.toLowerCase())
}

/**
 * Removes the `browser/` directory the built-in headless browser (removed) kept
 * its per-session files in, from a state volume an older image wrote. Only at
 * start, before any turn runs here.
 */
export async function removeLegacyBrowserDir(paths: HarnessPaths): Promise<void> {
  await rm(path.join(paths.stateDir, 'browser'), { recursive: true, force: true })
}

/** Where in a session's working directory its Claude Code task list is: a workspace path, so the write-back keeps it. */
export const TASK_LIST_DIR = '_claude/tasks'

/**
 * Points Claude Code's task list for the session at its working directory (plan 2026-10-13
 * Step 9.6). Measured on the bundled Claude Code (SDK 0.3.310, 2026-11-01): TaskCreate keeps
 * the list in `$CLAUDE_CONFIG_DIR/tasks/<session id>/<n>.json`, in neither the transcript nor
 * the cwd, and the config directory is the pod's own. So that path is made a link to
 * `<session dir>/_claude/tasks`: the files are then workspace files, written back with the
 * directory and refilled at any pod's placement. A list an older image kept there is moved in.
 * A placement drops dot-files (workspace/directory.ts), Claude Code's `.highwatermark` and
 * `.lock` here among them, so after a pod move a deleted task's id can be given again.
 */
export async function linkTaskList(paths: HarnessPaths, sessionDir: string, sessionId: string): Promise<void> {
  if (!isUuid(sessionId)) throw new StateDirError(`not a session id: ${JSON.stringify(sessionId)}`)
  const target = path.join(sessionDir, TASK_LIST_DIR)
  await mkdir(target, { recursive: true, mode: 0o700 })
  const link = path.join(claudeConfigDir(paths), 'tasks', sessionId.toLowerCase())
  await mkdir(path.dirname(link), { recursive: true })
  const st = await lstat(link).catch(() => undefined)
  if (st?.isSymbolicLink() && (await readlink(link)) === target) return
  if (st?.isDirectory()) {
    for (const name of await readdir(link)) {
      if (name.startsWith('.')) continue
      try {
        await copyFile(path.join(link, name), path.join(target, name), constants.COPYFILE_EXCL)
      } catch (err) {
        // One already in the directory is the newer copy. Any other failure keeps the old
        // list where it is, unlinked, rather than remove what was not moved.
        if ((err as NodeJS.ErrnoException).code === 'EEXIST') continue
        console.warn(`task list of session ${sessionId}: cannot move ${name} into its working directory (${(err as NodeJS.ErrnoException).code ?? String(err)}); left in ${link}`)
        return
      }
    }
  }
  if (st) await rm(link, { recursive: true, force: true })
  await symlink(target, link, 'dir')
}

/** Creates the session's working directory on this replica, idempotently. */
export async function ensureSessionDir(paths: HarnessPaths, sessionId: string): Promise<string> {
  const dir = sessionWorkDir(paths, sessionId)
  try {
    await mkdir(dir, { recursive: true, mode: 0o700 })
    // Its owner's only (workspace/directory.ts): mkdir's mode is not applied to a directory that was there.
    await chmod(dir, 0o700)
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code ?? String(err)
    throw new StateDirError(`cannot create the session working directory ${dir} (${code})`)
  }
  return dir
}

export async function ensureStateDirs(paths: HarnessPaths): Promise<string[]> {
  // The plugin package cache (#297) is checked with the others, so an
  // unwritable one stops the pod here rather than failing the first install.
  const dirs = [claudeConfigDir(paths), scratchDir(paths), pluginCacheDir(paths), ...(paths.sessionsDir ? [paths.sessionsDir] : [])]
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
          `mount a writable volume at ${dir === paths.sessionsDir ? dir : paths.stateDir} owned by the service user`,
      )
    }
  }
  return dirs
}
