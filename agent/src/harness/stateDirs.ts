import { constants } from 'node:fs'
import { access, mkdir } from 'node:fs/promises'
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
