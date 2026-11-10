import { z } from 'zod'
import { type NativeAccess, SANDBOXED_ACCESS, SHELL_BUILTINS } from '../access/nativeBuiltins.js'
import { BUILTIN_SOURCES } from '../access/model.js'
import type { RiskTier } from '../harness/permissions.js'
import { runInSandbox, type Sandboxes, type SyncLimits } from '../sandbox/client.js'
import { DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, type Shell } from '../sandbox/protocol.js'
import { wrapUntrustedText } from '../safety/untrusted.js'
import type { BuiltinResult } from './result.js'

// Bash and PowerShell in a sandbox pod (#5864, issue comment "Design"): the session's own tools of
// those names, served on its `builtin` server in place of Claude Code's, which would run in the
// worker pod beside the database URL and the KEK (rule #5295). Each call syncs the session's
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
  "a new pod (after 15 idle minutes, or 4 hours) starts from the session's files alone. The " +
  'network reaches only public hosts on ports 80 and 443, through an HTTPS proxy (HTTPS_PROXY is set; plain http:// and other ' +
  `protocols are refused). The timeout is ${DEFAULT_TIMEOUT_MS / 1000} s by default, at most ${MAX_TIMEOUT_MS / 1000} s. ` +
  `Output past ${OUTPUT_MAX_CHARS} characters is cut.`

export const SHELL_DESCRIPTIONS: Record<string, string> = {
  Bash: `Claude Code's Bash: runs a bash command and returns its output and exit code. ${where}`,
  PowerShell: `Claude Code's PowerShell: runs a PowerShell 7 (pwsh) command and returns its output and exit code. ${where}`,
}

function cut(text: string, room: number): { text: string; cut: number } {
  if (text.length <= room) return { text, cut: 0 }
  return { text: text.slice(0, Math.max(room, 0)), cut: text.length - Math.max(room, 0) }
}

/** Runs one Bash or PowerShell call. */
export async function runShell(ctx: ShellContext, shell: Shell, input: ShellInputArgs): Promise<BuiltinResult> {
  if (input.run_in_background) {
    return {
      text: `run_in_background is not available in this session: run the command in the foreground, with a timeout of up to ${MAX_TIMEOUT_MS / 1000} s.`,
      isError: true,
    }
  }
  const tool = shell === 'bash' ? 'Bash' : 'PowerShell'
  let run
  try {
    run = await runInSandbox({ ...ctx, limits: await ctx.limits() }, shell, input.command, input.timeout ?? DEFAULT_TIMEOUT_MS)
  } catch (err) {
    return { text: `${tool} could not run: the session's sandbox failed: ${err instanceof Error ? err.message : String(err)}`, isError: true }
  }
  const { result, back } = run
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
    ...(back.written.length ? { files_changed: back.written } : {}),
    ...(back.removed.length ? { files_removed: back.removed } : {}),
    ...(back.left.length ? { files_left_in_sandbox: back.left } : {}),
  }
  return { text: wrapUntrustedText(tool, 'the session sandbox', JSON.stringify(body)), ...(result.exit_code === 0 ? {} : { isError: true }) }
}
