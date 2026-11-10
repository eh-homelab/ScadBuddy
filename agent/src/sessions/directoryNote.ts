// The note a turn's prompt starts with when its transcript last ran in another directory (#5867).
// Its own module so memory/hindsight.ts can strip it from what it retains without importing the
// turn's assembly: it is ScadBuddy's text, never the user's words.

/** How the directory note starts (`directoryNote`). */
export const DIRECTORY_NOTE_MARK = "[System note] This session's working directory is now"

/**
 * What a turn's prompt starts with when the transcript it resumes last ran in another directory
 * (#5867): a fork's first turn (its parent's entries name the parent's directory), or a session
 * whose sessions root moved. Every session's cwd is its own `<root>/<id>/` (harness/stateDirs.ts
 * `sessionWorkDir`). One path for every session (`/workspace`) cannot be made: the warm sessions
 * on a worker pod share its filesystem, a per-process mount needs a mount namespace, which
 * AppArmor refuses there (`unshare --mount`, measured 2026-11-01, docs/ai/operating.md "Claude Code's own built-ins"), and
 * Claude Code takes `process.cwd()`, the real path (harness/confine.ts), so a link would not hide
 * the session's own directory either. Claude Code itself adds only "Primary working directory:
 * <new> (was <old>)" on such a turn (measured on the bundled CLI, test/forkCow.pg.test.ts); with
 * that alone the model still read the old absolute path first, and the seam refused it.
 */
export function directoryNote(from: string, to: string): string {
  return `${DIRECTORY_NOTE_MARK} ${to}. ${from}, the directory earlier messages name, is not this session's directory, and calls to it are refused: this session was forked from another one or moved. Use the same relative paths under ${to}.`
}

/**
 * The note's paragraph (it holds no line break), as memory/hindsight.ts strips it from each text
 * block or string on its own. Anchored to the very start, the only place `withNote` puts it, so a
 * user's line or paragraph that quotes its words after other text is kept.
 */
export const DIRECTORY_NOTE_RE = new RegExp(`^${DIRECTORY_NOTE_MARK.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[^\\n]*`)
