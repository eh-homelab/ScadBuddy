import { z } from 'zod'
import { type NativeAccess, SANDBOXED_ACCESS, SHELL_BUILTINS } from '../access/nativeBuiltins.js'
import { BUILTIN_SOURCES } from '../access/model.js'
import type { RiskTier } from '../harness/permissions.js'
import { findSandbox, runInSandbox, SandboxClient, type Sandboxes, type SlotRun, startInSandbox, type SyncBack, type SyncLimits } from '../sandbox/client.js'
import { DEFAULT_TIMEOUT_MS, MAX_SLOTS, MAX_TIMEOUT_MS, type Shell, SLOT_FILE_MAX_BYTES, SLOT_MAX_TIMEOUT_MS } from '../sandbox/protocol.js'
import { MAX_ACTIVE } from '../background/store.js'
import { wrapUntrustedText } from '../safety/untrusted.js'
import type { BackgroundContext } from './background.js'
import type { BuiltinResult } from './result.js'

// Bash and PowerShell in a sandbox pod (#5864, issue comment "Design"): the session's own tools of
// those names, served on its `builtin` server in place of Claude Code's, which would run in the
// worker pod beside the `agent` credential's proof and the Temporal client (rule #5295). Each call syncs the session's
// directory into the session's sandbox pod, runs the command there, and brings back what it
// changed (sandbox/client.ts). One switch governs each: the item `Bash` or `PowerShell` of
// `builtin:claude-code` (access/nativeBuiltins.ts), `outward` by default once a sandbox is set up.

export { SHELL_BUILTINS }
/** What the model gets of a command's output, as Claude Code's Bash keeps (30,000 characters). */
export const OUTPUT_MAX_CHARS = 30_000

export type ShellContext = {
  sandboxes: Sandboxes
  sessionId: string
  /** The session's directory on this worker: Claude Code's cwd, the copy that counts. */
  dir: string
  limits: () => Promise<SyncLimits>
  signal: AbortSignal
}

/**
 * The sandboxed Bash and PowerShell a turn offers, each at its tier: the items `Bash` and
 * `PowerShell` of `builtin:claude-code`, `outward` by default here (SANDBOXED_ACCESS), any not `off`.
 * `named` is each built-in the turn's allowed packages name, as for the native ones.
 */
export async function sandboxShellTools(
  access: NativeAccess,
  named: readonly { name: string; packages: readonly string[] }[],
): Promise<{ name: string; tier: RiskTier }[]> {
  const rows = access.rows ? await access.rows([BUILTIN_SOURCES.claudeCode]) : undefined
  const out: { name: string; tier: RiskTier }[] = []
  for (const name of SHELL_BUILTINS) {
    const now = await access.native(name, SANDBOXED_ACCESS, named.find((n) => n.name === name)?.packages ?? [], rows)
    if (now !== 'off') out.push({ name, tier: now })
  }
  return out
}

export const ShellInput = z.object({
  command: z.string().min(1),
  timeout: z.number().int().min(1).max(MAX_TIMEOUT_MS).optional(),
  description: z.string().optional(),
  run_in_background: z.boolean().optional(),
})
export type ShellInputArgs = z.infer<typeof ShellInput>

const where =
  'It runs in this session\'s sandbox, an isolated container with no secrets, in a directory that holds this session\'s files ' +
  '(the same files Read, Write and Edit see). Files it creates, changes or deletes there come back to the session after the ' +
  'command only at a path whose every name is letters, digits, "_", "." and "-", does not start with ".", and is at most 4 ' +
  'levels deep, outside node_modules and __pycache__, and only within the workspace caps. Any other file stays in the sandbox ' +
  'pod while the pod lasts, and the result lists it under files_left_in_sandbox (node_modules and __pycache__ are not listed); ' +
  "a new pod (after 15 idle minutes, or 4 hours) starts from the session's files alone. A session file past the workspace's " +
  'per-file cap is not in the sandbox, and the result lists it under files_not_in_sandbox, unless the sandbox keeps its own ' +
  'copy at that path (one an earlier command left there, listed then under files_left_in_sandbox), which stays and is not ' +
  'listed. The ' +
  'network reaches only public hosts on ports 80 and 443, through an HTTPS proxy (HTTPS_PROXY is set; plain http:// and other ' +
  `protocols are refused). The timeout is ${DEFAULT_TIMEOUT_MS / 1000} s by default, at most ${MAX_TIMEOUT_MS / 1000} s. ` +
  `Output past ${OUTPUT_MAX_CHARS} characters is cut. With run_in_background the command runs in the same sandbox past this call ` +
  `(no timeout; at most ${MAX_SLOTS} at once in the pod, and the pod's 4-hour lifetime); the answer names its task ID and an ` +
  `output file in the sandbox (its first ${SLOT_FILE_MAX_BYTES / (1024 * 1024)} MiB) that a later command can read. You are ` +
  'notified when it ends, with its exit code, the end of its output and the files it changed, once the session is idle. Its ' +
  'file changes come back to the session at its end and at each later Bash or PowerShell command, as a foreground command\'s ' +
  'do; where the session changed the same file since, the session\'s copy counts. TaskStop stops it.'

export const SHELL_DESCRIPTIONS: Record<string, string> = {
  Bash: `Claude Code's Bash: runs a bash command and returns its output and exit code. ${where}`,
  PowerShell: `Claude Code's PowerShell: runs a PowerShell 7 (pwsh) command and returns its output and exit code. ${where}`,
}

function cut(text: string, room: number): { text: string; cut: number } {
  if (text.length <= room) return { text, cut: 0 }
  return { text: text.slice(0, Math.max(room, 0)), cut: text.length - Math.max(room, 0) }
}

/**
 * How a background command starts and stops in the session's sandbox (#5916, #7310): Monitor's
 * `command` and Bash's `run_in_background` (BackgroundContext.sandbox).
 */
export type SandboxStarter = {
  start(shell: Shell, command: string, timeoutMs: number): Promise<SlotRun>
  /** Stops a slot this call started but could not keep (its task was refused). */
  stop(slot: string): Promise<void>
}

/** The starter over the session's sandbox (ShellContext). */
export function sandboxStarter(ctx: ShellContext): SandboxStarter {
  return {
    start: async (shell, command, timeoutMs) => startInSandbox({ ...ctx, limits: await ctx.limits() }, shell, command, timeoutMs),
    stop: async (slot) => {
      const signal = AbortSignal.timeout(30_000)
      const endpoint = await findSandbox(ctx.sandboxes, ctx.sessionId, signal).catch(() => undefined)
      if (endpoint) await new SandboxClient(endpoint, signal).stopSlot(slot).catch(() => undefined)
    },
  }
}

/** What a sync in brought back of an earlier background command's writes, as the result's fields. */
function pulledFields(pulled: SyncBack | undefined, overwritten: string[] | undefined): Record<string, unknown> {
  return {
    ...(pulled?.written.length ? { files_changed_in_background: pulled.written } : {}),
    ...(pulled?.removed.length ? { files_removed_in_background: pulled.removed } : {}),
    ...(pulled?.left.length ? { files_left_in_sandbox_by_background: pulled.left } : {}),
    ...(overwritten?.length
      ? {
          files_overwritten_in_sandbox: overwritten.map((path) => ({
            path,
            reason: "a background command changed it in the sandbox, and the session's copy changed too since; the session's copy counts",
          })),
        }
      : {}),
  }
}

/**
 * A Bash or PowerShell call with `run_in_background` (#7310): the command started in a background
 * slot of the session's sandbox, and a Monitor-kind task (spec.background `shell`) that follows
 * it from the replica that ran the turn and reports its end (background/sandboxSource.ts).
 */
async function runInBackground(ctx: ShellContext, shell: Shell, input: ShellInputArgs, background: BackgroundContext | undefined): Promise<BuiltinResult> {
  const tool = shell === 'bash' ? 'Bash' : 'PowerShell'
  if (!background?.commandQueue) {
    return {
      text: `run_in_background is not available in this session: it keeps no background tasks. Run the command in the foreground, with a timeout of up to ${MAX_TIMEOUT_MS / 1000} s.`,
      isError: true,
    }
  }
  const active = (await background.tasks.store.list(background.sessionId)).length
  if (active >= MAX_ACTIVE) return { text: `This session already has ${MAX_ACTIVE} background tasks running; stop one with TaskStop first.`, isError: true }
  const sandbox = sandboxStarter(ctx)
  let run: SlotRun
  try {
    run = await sandbox.start(shell, input.command, SLOT_MAX_TIMEOUT_MS)
  } catch (err) {
    return { text: `${tool} could not start the command: the session's sandbox failed: ${err instanceof Error ? err.message : String(err)}`, isError: true }
  }
  const description = (input.description ?? input.command).slice(0, 500)
  let t
  try {
    t = await background.tasks.create({
      sessionId: background.sessionId,
      kind: 'monitor',
      description,
      spec: {
        description,
        command: input.command,
        queue: background.commandQueue,
        timeoutMs: SLOT_MAX_TIMEOUT_MS,
        background: 'shell',
        sandbox: { slot: run.slot.id, shell, outputFile: run.slot.output_file },
      },
      createdBy: background.owner,
      expiresAt: new Date(background.now().getTime() + SLOT_MAX_TIMEOUT_MS),
    })
  } catch (err) {
    await sandbox.stop(run.slot.id)
    throw err
  }
  if (!t) {
    await sandbox.stop(run.slot.id)
    return { text: `This session already has ${MAX_ACTIVE} background tasks running; stop one with TaskStop first.`, isError: true }
  }
  const extra = { ...pulledFields(run.pulled, run.overwritten), ...(run.notSent.length ? { files_not_in_sandbox: run.notSent } : {}) }
  return {
    text:
      `Command running in background with ID: ${t.id}. Output is being written to: ${run.slot.output_file} (a file in this ` +
      'session\'s sandbox pod, not in the session directory: read it with Bash, for example `tail -n 50` on it, while the pod lasts). ' +
      'You will be notified when it completes, with its exit code, the end of its output and the files it changed, once this turn ' +
      'has ended and the session is idle. It is running now: do not start a second copy while it runs. If it is no longer ' +
      `needed, stop it with TaskStop ${t.id}.` +
      (Object.keys(extra).length ? `\n${wrapUntrustedText(tool, 'the session sandbox', JSON.stringify(extra))}` : ''),
  }
}

/** Runs one Bash or PowerShell call; `background`, the session's background tasks, for `run_in_background`. */
export async function runShell(ctx: ShellContext, shell: Shell, input: ShellInputArgs, background?: BackgroundContext): Promise<BuiltinResult> {
  if (input.run_in_background) return runInBackground(ctx, shell, input, background)
  const tool = shell === 'bash' ? 'Bash' : 'PowerShell'
  let run
  let limits: SyncLimits
  try {
    limits = await ctx.limits()
    run = await runInSandbox({ ...ctx, limits }, shell, input.command, input.timeout ?? DEFAULT_TIMEOUT_MS)
  } catch (err) {
    return { text: `${tool} could not run: the session's sandbox failed: ${err instanceof Error ? err.message : String(err)}`, isError: true }
  }
  const { result, back, notSent, pulled, overwritten } = run
  const out = cut(result.stdout, OUTPUT_MAX_CHARS)
  const errText = cut(result.stderr, OUTPUT_MAX_CHARS - out.text.length)
  const body = {
    exit_code: result.exit_code,
    ...(result.signal ? { signal: result.signal } : {}),
    ...(result.timed_out ? { timed_out: `the command was killed after ${input.timeout ?? DEFAULT_TIMEOUT_MS} ms` } : {}),
    stdout: out.text,
    stderr: errText.text,
    ...(out.cut > 0 || result.stdout_bytes > Buffer.byteLength(result.stdout)
      ? { stdout_cut: `${result.stdout_bytes} bytes in all; the rest is not shown` }
      : {}),
    ...(errText.cut > 0 || result.stderr_bytes > Buffer.byteLength(result.stderr)
      ? { stderr_cut: `${result.stderr_bytes} bytes in all; the rest is not shown` }
      : {}),
    ...pulledFields(pulled, overwritten),
    ...(back.written.length ? { files_changed: back.written } : {}),
    ...(back.removed.length ? { files_removed: back.removed } : {}),
    ...(back.left.length ? { files_left_in_sandbox: back.left } : {}),
    ...(notSent.length
      ? {
          files_not_in_sandbox: notSent.map((path) => ({
            path,
            reason: `it is past the workspace's per-file cap of ${limits.fileMaxBytes} bytes, so it was not sent to the sandbox and the command did not see it`,
          })),
        }
      : {}),
  }
  return { text: wrapUntrustedText(tool, 'the session sandbox', JSON.stringify(body)), ...(result.exit_code === 0 ? {} : { isError: true }) }
}
