import { StringDecoder } from 'node:string_decoder'
import { setTimeout as sleep } from 'node:timers/promises'
import { catchUpInSandbox, findSandbox, SandboxClient, type Sandboxes, type SyncBack, type SyncLimits } from '../sandbox/client.js'
import type { SlotStatus } from '../sandbox/protocol.js'
import { EVENT_CHARS, eventBatcher, type EventSink, type MonitorEnd } from './monitor.js'
import type { BackgroundTask } from './store.js'

// A background command in the session's sandbox pod (#5916, #7310): a Monitor's `command`, or a
// Bash or PowerShell call with `run_in_background`. The command runs in a background slot of the
// pod's exec server (sandbox/slots.ts), started by the tool in the turn (sandbox/client.ts
// `startInSandbox`); this source, the monitor's heartbeating activity on the replica that ran the
// turn (temporal/backgroundTask.ts `monitor`), follows its output. Nothing of the command runs on
// the worker: only its output and its status come back here, checked (client.ts), and its files
// at its end, through the same sync and the same caps as a foreground command's.

/** A task's background command in the sandbox: `spec.sandbox`, set by builtins/background.ts and builtins/sandboxShell.ts. */
export type SandboxSpec = { slot: string; shell: 'bash' | 'pwsh'; outputFile: string }

/** The spec's sandbox part, or undefined for a command that runs on the worker (or a WebSocket). */
export function sandboxSpec(t: Pick<BackgroundTask, 'spec'>): SandboxSpec | undefined {
  const s = t.spec.sandbox as Partial<SandboxSpec> | undefined
  if (typeof s !== 'object' || s === null || typeof s.slot !== 'string') return undefined
  return { slot: s.slot, shell: s.shell === 'pwsh' ? 'pwsh' : 'bash', outputFile: String(s.outputFile ?? '') }
}

/** Whether the task is a Bash or PowerShell call's `run_in_background` (else a Monitor's command). */
export const isBackgroundShell = (t: Pick<BackgroundTask, 'spec'>): boolean => t.spec.background === 'shell'

/** How much of a background shell's output its end notice shows (its last characters). */
export const SHELL_TAIL_CHARS = EVENT_CHARS
/** How often a broken watch is opened again before the source fails. */
const WATCH_RETRIES = 3

export type SandboxSourceDeps = {
  sandboxes: Sandboxes
  limits: () => Promise<SyncLimits>
}

/** Stops the task's slot in the pod, if the pod still has it; never throws (a pod that is gone has stopped it). */
export async function stopSlot(deps: SandboxSourceDeps, t: Pick<BackgroundTask, 'sessionId' | 'spec'>): Promise<SlotStatus | undefined> {
  const spec = sandboxSpec(t)
  if (!spec) return undefined
  try {
    const signal = AbortSignal.timeout(30_000)
    const endpoint = await findSandbox(deps.sandboxes, t.sessionId, signal)
    return endpoint ? await new SandboxClient(endpoint, signal).stopSlot(spec.slot) : undefined
  } catch {
    return undefined
  }
}

/** Keeps the last `max` characters of what is pushed. */
function tailKeeper(max: number): { push(text: string): void; text(): string; cut: () => number } {
  let tail = ''
  let cut = 0
  return {
    push(text) {
      tail += text
      if (tail.length > max * 2) {
        cut += tail.length - max
        tail = tail.slice(-max)
      }
    },
    text: () => (tail.length > max ? tail.slice(-max) : tail),
    cut: () => cut + Math.max(tail.length - max, 0),
  }
}

/** What a background shell's end says (Claude Code's notice of a background command, with its output's tail and its files). */
export function shellNotice(
  t: Pick<BackgroundTask, 'id' | 'description' | 'spec'>,
  end: MonitorEnd,
  output?: { tail: string; cut: number },
  files?: SyncBack,
): string {
  const description = String(t.spec.description ?? t.description)
  const outputFile = sandboxSpec(t)?.outputFile ?? ''
  let status: 'completed' | 'failed' | 'killed'
  let summary: string
  const name = `Background command "${description}"`
  switch (end.outcome) {
    case 'exited':
      status = end.code === 0 ? 'completed' : 'failed'
      summary = end.code === 0 ? `${name} completed (exit code 0)` : `${name} failed with ${end.code === null ? `signal ${end.signal ?? 'unknown'}` : `exit code ${end.code}`}`
      break
    case 'expired':
      status = 'killed'
      summary = `${name} was killed: it reached the sandbox pod's 4-hour lifetime`
      break
    case 'cancelled':
      status = 'killed'
      summary = `${name} was stopped`
      break
    case 'failed':
      status = 'failed'
      summary = `${name} failed: ${end.reason}`
      break
    default:
      status = 'killed'
      summary = `${name} ended (${end.outcome})`
  }
  const out = output?.tail
    ? `<output>${output.cut > 0 ? `[the first ${output.cut} characters are not shown]\n` : ''}${output.tail}</output>\n`
    : ''
  const changed = files?.written.length ? `<files-changed>${files.written.join(', ')}</files-changed>\n` : ''
  const removed = files?.removed.length ? `<files-removed>${files.removed.join(', ')}</files-removed>\n` : ''
  const left = files?.left.length ? `<files-left-in-sandbox>${files.left.map((l) => `${l.path}: ${l.reason}`).join('; ')}</files-left-in-sandbox>\n` : ''
  return (
    `<task-notification>\n<task-id>${t.id}</task-id>\n<output-file>${outputFile} (in the session's sandbox pod, while it lasts)</output-file>\n` +
    `<status>${status}</status>\n<summary>${summary}</summary>\n${out}${changed}${removed}${left}</task-notification>`
  )
}

/**
 * Follows the task's slot until it ends, the task is cancelled (`signal`), it makes too many
 * events, or the pod ends; then stops the slot (if it still runs) and brings back what it wrote
 * (catchUpInSandbox). A Monitor's stdout lines are its events, batched as a command's on the
 * worker (eventBatcher); a background shell makes one event, its end notice. `timeoutMs` is the
 * slot's own timeout (the pod kills it then); one minute past it, the watch stops it too.
 */
export async function followSlot(
  deps: SandboxSourceDeps,
  t: BackgroundTask,
  options: { dir: string; timeoutMs: number; signal: AbortSignal; onEvent: EventSink; retryMs?: number },
): Promise<MonitorEnd> {
  const spec = sandboxSpec(t)
  if (!spec) return { outcome: 'failed', reason: 'the task names no sandbox slot' }
  const shell = isBackgroundShell(t)
  const stop = new AbortController()
  let ended: MonitorEnd | undefined
  const stopWith = (why: MonitorEnd) => {
    ended ??= why
    stop.abort()
  }
  const watchSignal = AbortSignal.any([options.signal, stop.signal])
  const events = shell ? undefined : eventBatcher({ onEvent: options.onEvent, stop: stopWith })
  const stdout = new StringDecoder('utf8')
  const stderr = new StringDecoder('utf8')
  const tail = tailKeeper(SHELL_TAIL_CHARS)
  const guard = setTimeout(() => stopWith({ outcome: 'expired' }), options.timeoutMs + 60_000)
  let status: SlotStatus | undefined
  let from = 0
  try {
    for (let failures = 0; status === undefined && !watchSignal.aborted; ) {
      const endpoint = await findSandbox(deps.sandboxes, t.sessionId, watchSignal).catch(() => undefined)
      if (watchSignal.aborted) break
      if (!endpoint) {
        stopWith({ outcome: 'failed', reason: "the sandbox pod ended (idle, or past its 4-hour lifetime) while the command ran" })
        break
      }
      try {
        for await (const event of new SandboxClient(endpoint, watchSignal).watch(spec.slot, from)) {
          failures = 0
          if (event.type === 'gone') {
            stopWith({ outcome: 'failed', reason: 'the sandbox pod no longer has the command: the pod ended, or the command was stopped there' })
            break
          }
          if (event.type === 'end') {
            status = event.status
            break
          }
          if (event.type === 'lost') {
            from = event.to
            const note = `[${event.to - event.from} bytes of output not kept]\n`
            if (shell) tail.push(note)
            else events!.push(note)
            continue
          }
          // A pod may resend bytes it sent before (a retried watch): only what is past `from` counts.
          const skip = Math.max(from - event.at, 0)
          if (skip >= event.data.length) continue
          const data = event.data.subarray(skip)
          from = event.at + skip + data.length
          if (shell) tail.push((event.stream === 'stdout' ? stdout : stderr).write(data))
          else if (event.stream === 'stdout') events!.push(stdout.write(data))
        }
      } catch (err) {
        if (watchSignal.aborted) break
        if (++failures > WATCH_RETRIES) {
          stopWith({ outcome: 'failed', reason: `the sandbox's watch failed: ${err instanceof Error ? err.message : String(err)}` })
          break
        }
        deps.sandboxes.release?.(t.sessionId, endpoint)
        await sleep(options.retryMs ?? 1000, undefined, { signal: watchSignal }).catch(() => {})
      }
    }
  } finally {
    clearTimeout(guard)
  }
  if (events) {
    events.push(stdout.end())
    await events.end()
  } else tail.push(stdout.end() + stderr.end())
  // Ended any other way than by itself: the slot must not run on in the pod.
  if (status === undefined) status = await stopSlot(deps, t)
  const end: MonitorEnd = options.signal.aborted
    ? { outcome: 'cancelled' }
    : (ended ??
      (status === undefined
        ? { outcome: 'failed', reason: 'the command ended with no status from the sandbox' }
        : status.timed_out
          ? { outcome: 'expired' }
          : status.stopped
            ? { outcome: 'failed', reason: 'the sandbox stopped the command: no watch followed it' }
            : { outcome: 'exited', code: status.exit_code, ...(status.signal ? { signal: status.signal } : {}) }))
  // What the command wrote comes back to the session, as a foreground command's does.
  let files: SyncBack | undefined
  try {
    files = await catchUpInSandbox({ sandboxes: deps.sandboxes, sessionId: t.sessionId, dir: options.dir, limits: await deps.limits(), signal: AbortSignal.timeout(120_000) })
  } catch (err) {
    console.warn(`session ${t.sessionId}: background command ${t.id}'s files could not be brought back: ${err instanceof Error ? err.message : String(err)}`)
  }
  if (shell) await options.onEvent(shellNotice(t, end, { tail: tail.text(), cut: tail.cut() }, files)).catch(() => false)
  return end
}
