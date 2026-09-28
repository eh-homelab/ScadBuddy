import { constants } from 'node:fs'
import { access, mkdir } from 'node:fs/promises'
import path from 'node:path'
import { claudeConfigDir, scratchDir, type HarnessPaths } from './options.js'

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

/**
 * A session's own working directory (#300, spec §6): `work/sessions/<id>`.
 * Deterministic from the session id, so every replica computes the same path
 * and a session's `cwd` is the same wherever it resumes (spec §3.1: the SDK's
 * store key "derives from the working directory"). The id must be a UUID, which
 * the session manager guarantees; anything else is refused so it can never
 * escape `work/`.
 */
export function sessionWorkDir(paths: HarnessPaths, sessionId: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sessionId)) {
    throw new StateDirError(`not a session id: ${JSON.stringify(sessionId)}`)
  }
  return path.join(scratchDir(paths), 'sessions', sessionId.toLowerCase())
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
  const dirs = [claudeConfigDir(paths), scratchDir(paths)]
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
