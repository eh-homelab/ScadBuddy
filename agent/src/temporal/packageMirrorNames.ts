// The names the PackageMirror workflow (packageMirror.ts) shares with its activity and its
// starters (plugins/packages/mirrorJob.ts), kept apart so Node code imports no workflow code.

/** The workflow type. */
export const PACKAGE_MIRROR_WORKFLOW = 'PackageMirror'
/** Its one activity. */
export const PACKAGE_MIRROR_ACTIVITY = 'package_mirror'
/** The failure type of a fetch that gave other files than the pin's: never retried. */
export const MIRROR_REFUSED = 'PackageMirrorRefused'

export type PackageMirrorInput = { name: string; commit: string; contentHash: string }

/**
 * `kept`: the recorded mirror read back and hashed to the pin, so nothing was fetched;
 * `mirrored`: fetched, checked, written and recorded; `not-pinned`: the row no longer names
 * this commit and content hash (re-pinned, discarded or removed), so there is nothing to make.
 */
export type PackageMirrorResult = { outcome: 'kept' | 'mirrored' | 'not-pinned'; sha256?: string }
