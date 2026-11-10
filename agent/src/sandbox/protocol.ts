// The sandbox's HTTP API (#5864), shared by the server in the sandbox pod (server.ts) and the
// agent's client (client.ts). This file and the server import only Node's built-ins and
// http/egress.ts, so the `sandbox` image carries no node_modules.

/** The port the exec server listens on in its pod. */
export const SANDBOX_PORT = 8090
/** The port the egress proxy listens on. */
export const EGRESS_PORT = 3128

/** The shells a command runs in. */
export const SHELLS = ['bash', 'pwsh'] as const
export type Shell = (typeof SHELLS)[number]

/** Claude Code's Bash timeout: 2 minutes by default, 10 at most. */
export const DEFAULT_TIMEOUT_MS = 120_000
export const MAX_TIMEOUT_MS = 600_000
/** What the server keeps of each stream; the rest is counted, not kept. */
export const STREAM_MAX_BYTES = 1024 * 1024
/** How many processes the command's uid may have (`ulimit -u`): the pod's user namespace makes it the pod's own. */
export const MAX_PROCESSES = 512

/** A segment of a workspace path (workspace/store.ts WORKSPACE_PATH's). */
const SEGMENT = '[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'
const WORKSPACE_PATH = new RegExp(`^${SEGMENT}(/${SEGMENT}){0,3}$`)
const WORKSPACE_PATH_MAX = 512
/** A segment alone, for a walk. */
export const SEGMENT_RE = new RegExp(`^${SEGMENT}$`)
/** How deep a workspace path goes. */
export const MAX_SEGMENTS = 4
/** Directory names whose files never sync (workspace/directory.ts SCRATCH): they stay in the pod. */
export const SCRATCH_DIRS: ReadonlySet<string> = new Set(['node_modules', '__pycache__'])
/** How many entries a manifest walk visits before it stops and says it is truncated. */
export const MANIFEST_MAX_ENTRIES = 20_000

/** Whether `rel` is a workspace path that syncs: the workspace's form, and no scratch directory in it. */
export function syncPath(rel: string): boolean {
  if (rel.length === 0 || rel.length > WORKSPACE_PATH_MAX || !WORKSPACE_PATH.test(rel)) return false
  return !rel.split('/').slice(0, -1).some((part) => SCRATCH_DIRS.has(part))
}

export type ManifestEntry = { path: string; sha256: string; size: number }
/** How many paths that never sync a manifest names (`unsynced`). */
export const UNSYNCED_MAX = 50
/**
 * GET /v1/manifest: every regular file at a sync path under the work directory, and up to
 * UNSYNCED_MAX paths that hold files but never sync (a name a workspace path cannot take, or
 * deeper than MAX_SEGMENTS; a directory ends in `/`). Scratch trees are not named.
 */
export type Manifest = { files: ManifestEntry[]; truncated: boolean; unsynced?: string[] }

/**
 * GET and PUT /v1/kept: the files a sync back left in the pod past a cap, by path, with what the
 * session directory held at that path then (its sha256, `large`, or null for none). The exec
 * server keeps it in its own memory, outside /work, so every worker's sync in sees it (client.ts
 * `Kept`); it is as untrusted as the rest of what the pod says.
 */
export type KeptRecord = Record<string, string | null>
/** A KeptRecord value: a sha256, `large`, or null. */
export function keptValue(value: unknown): value is string | null {
  return value === null || value === 'large' || (typeof value === 'string' && /^[0-9a-f]{64}$/.test(value))
}

/** POST /v1/exec. */
export type ExecRequest = { shell: Shell; command: string; timeout_ms: number }
export type ExecResult = {
  exit_code: number | null
  signal: string | null
  timed_out: boolean
  stdout: string
  stderr: string
  /** Each stream's whole length; past STREAM_MAX_BYTES only that much is in the text. */
  stdout_bytes: number
  stderr_bytes: number
  duration_ms: number
}

/** The header that carries a file's sha256 on PUT and GET /v1/files/<path>. */
export const SHA_HEADER = 'x-sandbox-sha256'
