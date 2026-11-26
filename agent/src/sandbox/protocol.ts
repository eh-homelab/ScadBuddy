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
/**
 * The most a PUT of a path record (the base, the kept record) may be: MANIFEST_MAX_ENTRIES
 * entries of `"<path>":"<sha256>",`, each path at most WORKSPACE_PATH_MAX characters that JSON
 * never escapes (WORKSPACE_PATH), so a full manifest's record always fits (about 11.6 MiB).
 */
export const RECORD_MAX_BYTES = MANIFEST_MAX_ENTRIES * (WORKSPACE_PATH_MAX + 72) + 64

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
 * session directory held at that path then (its sha256, `large` when it could not be hashed, or
 * null for none). The exec
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

// Background slots (#5916, #7310): a command that runs past its call (a Monitor's command, or a
// Bash or PowerShell call with `run_in_background`) in its own process group, beside the
// foreground commands. The sweep after a command (server.ts reapStrays) spares a running slot's
// group, and only that group: a process that leaves it (`setsid`, a double fork) is killed at the
// next sweep. A slot has the same uid, `ulimit -u`, environment, egress proxy and /work as a
// foreground command.

/** How many slots may run at once in one sandbox pod; a start past it is refused (409). */
export const MAX_SLOTS = 4
/** What a slot keeps of its output, both streams, the newest bytes; a watch that asks for older ones is told they are lost. */
export const SLOT_KEEP_BYTES = 1024 * 1024
/** A slot's longest run: the pod's own lifetime (pods.ts ACTIVE_DEADLINE_S). */
export const SLOT_MAX_TIMEOUT_MS = 4 * 60 * 60 * 1000
/** What a slot writes of its output to its output file in the pod (`output_file`); past it, the file says so and stops. */
export const SLOT_FILE_MAX_BYTES = 16 * 1024 * 1024
/** How often a watch sends a heartbeat line while nothing else happens. */
export const WATCH_HEARTBEAT_MS = 10_000
/**
 * A slot no watch has followed for this long is stopped: its watcher (a worker's activity) is gone,
 * so nothing would report or stop it. Longer than the activity may wait to start (its
 * schedule-to-start timeout is 5 minutes, temporal/backgroundTask.ts).
 */
export const SLOT_UNWATCHED_MS = 6 * 60_000
/** How long an ended slot is kept for its status and watch before it is forgotten. */
export const SLOT_ENDED_KEEP_MS = 10 * 60_000
/** A slot's id: the server makes it, 32 lowercase hex digits; any other id is refused. */
export const SLOT_ID_RE = /^[0-9a-f]{32}$/
/** The most raw bytes one watch `data` frame carries. */
export const WATCH_CHUNK_BYTES = 32 * 1024

/** POST /v1/slots. */
export type SlotRequest = { shell: Shell; command: string; timeout_ms: number }
/** POST /v1/slots' answer: the slot's id and the file in the pod its output is written to. */
export type SlotStarted = { id: string; output_file: string }
/** GET /v1/slots/<id>, and the end of a watch. */
export type SlotStatus = {
  id: string
  running: boolean
  exit_code: number | null
  signal: string | null
  timed_out: boolean
  /** Stopped by DELETE /v1/slots/<id>, or because no watch followed it for SLOT_UNWATCHED_MS. */
  stopped: boolean
  /** Every byte of output so far, both streams. */
  bytes: number
  output_file: string
}
/**
 * One line of GET /v1/slots/<id>/watch?from=<offset> (a JSON line each): output from byte `at`
 * of the slot's output, whose `bytes` raw bytes follow the line at once (no encoding), a range no
 * longer kept, a heartbeat, or the end.
 */
export type WatchLine =
  | { type: 'data'; at: number; stream: 'stdout' | 'stderr'; bytes: number }
  | { type: 'lost'; from: number; to: number }
  | { type: 'beat' }
  | { type: 'end'; status: SlotStatus }

/**
 * GET and PUT /v1/base: the pod's files by path (their sha256) as the last sync left them, or null
 * before a sync set it. A file that differs from it changed in the pod since (a slot wrote it),
 * so the next sync in brings it back to the session rather than overwriting it (client.ts
 * syncIn). As untrusted as the rest of what the pod says: it can only make a sync bring back
 * what a command could have written anyway.
 */
export type BaseRecord = { base: Record<string, string> | null }
