import type { BackgroundTasks } from '../background/service.js'
import { checkCron, CRON_EXPIRY_MS, CronError, describeCron, nextFire, timeOfDay, WAKEUP_MAX_S, WAKEUP_MIN_S } from '../background/schedule.js'
import { checkSocketUrl, MONITOR_DEFAULT_MS, MONITOR_MAX_MS, shortDuration } from '../background/monitor.js'
import { type BackgroundTask, MAX_ACTIVE } from '../background/store.js'
import type { Owner } from '../sessions/protocol.js'
import type { SessionFiles } from '../workspace/store.js'
import { workflowScriptPath, workspaceRelative } from '../background/activities.js'
import { readScript } from '../background/workflowRunner.js'
import { WORKFLOW_CONCURRENCY, WORKFLOW_MAX_AGENTS } from '../background/workflowScript.js'
import type { BuiltinResult } from './files.js'

// Claude Code's background built-ins, as the session keeps them (src/background/):
// CronCreate, CronDelete, CronList, ScheduleWakeup and TaskStop. Claude Code keeps its
// jobs in its own process ("Session-only (not written to disk, dies when Claude
// exits)"), which a turn ends; these last as long as the session, survive restarts, and
// fire the prompt as the session's next message once it is idle. Their answers are
// Claude Code's (measured on 2.1.296 in a classic turn), saying so where it differs.

export type BackgroundContext = {
  tasks: BackgroundTasks
  sessionId: string
  /** Who owns the turn: a fire runs as them, while they still own the session. */
  owner: Owner
  tz: string
  now: () => Date
  /**
   * The queue a Monitor's command runs on: where this session's Bash runs (the replica
   * that ran a classic turn, agent-durable's for a durable one). None: no commands.
   */
  commandQueue?: string | undefined
  /** The session workspace: a Workflow's script is kept there, and a scriptPath read from it. */
  workspace?: Pick<SessionFiles, 'read' | 'write'> | undefined
}

const error = (text: string): BuiltinResult => ({ text, isError: true })
const KEPT = 'Kept by the session (it survives restarts, fires once the session is idle, and the ScadBuddy panel lists it).'
const full = () =>
  error(`This session already has ${MAX_ACTIVE} background tasks running; stop one with CronDelete or TaskStop first.`)

const recurringOf = (t: BackgroundTask) => t.spec.recurring !== false

export async function cronCreate(
  ctx: BackgroundContext,
  input: { cron: string; prompt: string; recurring?: boolean; durable?: boolean },
): Promise<BuiltinResult> {
  const recurring = input.recurring !== false
  const now = ctx.now()
  let first: Date | undefined
  try {
    checkCron(input.cron)
    first = nextFire(input.cron, now, ctx.tz)
  } catch (err) {
    if (err instanceof CronError) return error(err.message)
    throw err
  }
  if (!first) return error(`Cron expression '${input.cron}' never fires.`)
  const t = await ctx.tasks.create({
    sessionId: ctx.sessionId,
    kind: 'cron',
    description: input.prompt.slice(0, 500) || input.cron,
    spec: { cron: input.cron.trim(), prompt: input.prompt, recurring, tz: ctx.tz },
    createdBy: ctx.owner,
    nextAt: first,
    ...(recurring ? { expiresAt: new Date(now.getTime() + CRON_EXPIRY_MS) } : {}),
  })
  if (!t) return full()
  const when = describeCron(input.cron)
  const zone = `Times are in ${ctx.tz}.`
  // A recurring job whose first occurrence is past its 7 days (a monthly or yearly one) ends at once.
  if (t.state !== 'active') return error(`Recurring job ${t.id} (${when}) was not kept: ${t.detail ?? 'it ended'}. ${zone}`)
  return {
    text: recurring
      ? `Scheduled recurring job ${t.id} (${when}). ${KEPT} Auto-expires after 7 days. Use CronDelete to cancel sooner. ${zone}`
      : `Scheduled one-shot task ${t.id} (${when}). ${KEPT} It will fire once then auto-delete. ${zone}`,
  }
}

export async function cronDelete(ctx: BackgroundContext, input: { id: string }): Promise<BuiltinResult> {
  const t = await ctx.tasks.get(ctx.sessionId, input.id)
  if (!t || t.kind !== 'cron' || !(await ctx.tasks.cancel(ctx.sessionId, input.id, 'CronDelete'))) {
    return error(`No scheduled job with id '${input.id}'`)
  }
  return { text: `Cancelled job ${input.id}.` }
}

export async function cronList(ctx: BackgroundContext): Promise<BuiltinResult> {
  const jobs = (await ctx.tasks.store.list(ctx.sessionId)).filter((t) => t.kind === 'cron').reverse()
  if (!jobs.length) return { text: 'No scheduled jobs.' }
  return {
    text: jobs
      .map((t) => `${t.id} — ${describeCron(String(t.spec.cron))} (${recurringOf(t) ? 'recurring' : 'one-shot'}) [session]: ${String(t.spec.prompt)}`)
      .join('\n'),
  }
}

/** The session's pending wakeups, cancelled; how many there were. */
async function cancelWakeups(ctx: BackgroundContext, by: string): Promise<number> {
  let n = 0
  for (const t of await ctx.tasks.store.list(ctx.sessionId)) {
    if (t.kind === 'wakeup' && (await ctx.tasks.cancel(ctx.sessionId, t.id, by))) n += 1
  }
  return n
}

export async function scheduleWakeup(
  ctx: BackgroundContext,
  input: { delaySeconds?: number; reason?: string; prompt?: string; stop?: boolean; noop?: boolean },
): Promise<BuiltinResult> {
  if (input.stop) {
    const n = await cancelWakeups(ctx, 'ScheduleWakeup stop')
    return {
      text:
        `Loop stopped — cancelled ${n} pending wakeup(s); no further dynamic-loop wakeups scheduled. If you armed a Monitor ` +
        "for this loop, TaskStop it now. Then write the loop's outcome for the user as ordinary visible response text — and end the turn.",
    }
  }
  if (input.delaySeconds === undefined || !input.prompt || !input.reason) {
    return error('delaySeconds, reason and prompt are required unless stop is true.')
  }
  const delay = Math.round(Math.min(WAKEUP_MAX_S, Math.max(WAKEUP_MIN_S, input.delaySeconds)))
  // One wakeup at a time: the new one replaces any still pending, as a loop re-arms.
  await cancelWakeups(ctx, 'a newer ScheduleWakeup')
  const at = new Date(ctx.now().getTime() + delay * 1000)
  const t = await ctx.tasks.create({
    sessionId: ctx.sessionId,
    kind: 'wakeup',
    description: input.reason.slice(0, 500),
    spec: { prompt: input.prompt, reason: input.reason, delaySeconds: delay, at: at.toISOString(), noop: input.noop ?? null },
    createdBy: ctx.owner,
    nextAt: at,
  })
  if (!t) return full()
  return {
    text:
      `Next wakeup scheduled for ${timeOfDay(at, ctx.tz)} ${ctx.tz} (in ${delay}s). If you owe the user a status update this tick, ` +
      'write it now as ordinary response text; then end the turn — the session wakes you when the wakeup fires.',
  }
}

export async function taskStop(ctx: BackgroundContext, input: { task_id?: string; shell_id?: string }): Promise<BuiltinResult> {
  const id = input.task_id ?? input.shell_id ?? ''
  const stopped = await ctx.tasks.cancel(ctx.sessionId, id, 'TaskStop')
  if (!stopped) return error(`No task found with ID: ${id}`)
  return {
    text: JSON.stringify({
      message: `Successfully stopped task: ${id} (${stopped.kind === 'monitor' ? String(stopped.spec.command ?? (stopped.spec.ws as { url?: string } | undefined)?.url ?? stopped.description) : stopped.description})`,
      task_id: id,
      task_type: stopped.kind,
    }),
  }
}

/** A subprotocol is an RFC 7230 token (RFC 6455 §4.1; schemas.json's Monitor `ws.protocols` pattern). */
const SUBPROTOCOL = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/

export async function monitor(
  ctx: BackgroundContext,
  input: { description: string; timeout_ms?: number; command?: string; ws?: { url: string; protocols?: string[] } },
): Promise<BuiltinResult> {
  if ((input.command === undefined) === (input.ws === undefined)) return error('Give exactly one of command and ws.')
  if (input.command !== undefined && !ctx.commandQueue) return error('This session cannot run monitor commands; a ws source works.')
  if (input.ws) {
    // schemas.json's token pattern: a bad one would otherwise fail only once `new WebSocket` throws.
    const bad = input.ws.protocols?.find((p) => !SUBPROTOCOL.test(p))
    if (bad !== undefined) return error(`${JSON.stringify(bad)} is not a WebSocket subprotocol: use only token characters.`)
    const refused = await checkSocketUrl(input.ws.url)
    if (refused) return error(refused)
  }
  const timeoutMs = Math.round(Math.min(MONITOR_MAX_MS, Math.max(1000, input.timeout_ms ?? MONITOR_DEFAULT_MS)))
  const t = await ctx.tasks.create({
    sessionId: ctx.sessionId,
    kind: 'monitor',
    description: input.description.slice(0, 500) || 'monitor',
    spec: {
      description: input.description,
      timeoutMs,
      ...(input.command !== undefined ? { command: input.command, queue: ctx.commandQueue } : { ws: input.ws }),
    },
    createdBy: ctx.owner,
    expiresAt: new Date(ctx.now().getTime() + timeoutMs),
  })
  if (!t) return full()
  return {
    text:
      `Monitor started (task ${t.id}, expires in ${shortDuration(timeoutMs)} unless the source ends first; you get one notice at expiry — ` +
      're-arm if you still need the watch). You will be notified on each event: the session sends its events to you as a new ' +
      'message once this turn has ended and it is idle. Keep working — do not poll or sleep. Events may arrive while you are ' +
      'waiting for the user — an event is not their reply.',
  }
}

/** How long a Workflow may run before it is stopped. */
export const WORKFLOW_EXPIRY_MS = 6 * 60 * 60 * 1000

export async function workflow(
  ctx: BackgroundContext,
  input: { script?: string; scriptPath?: string; name?: string; args?: unknown; resumeFromRunId?: string },
): Promise<BuiltinResult> {
  const ws = { kind: 'session' as const, sessionId: ctx.sessionId }
  let script = input.script
  if (script === undefined && input.scriptPath !== undefined) {
    const rel = workspaceRelative(input.scriptPath)
    const file = rel && ctx.workspace ? await ctx.workspace.read(ws, rel) : undefined
    if (!file) return error(`No workflow script at ${input.scriptPath}: give a path under /workspace, as a Workflow's result names it.`)
    script = file.bytes.toString('utf8')
  }
  if (script === undefined) {
    return error(
      input.name !== undefined
        ? `No saved workflow named "${input.name}": this session has no saved workflows; pass the script inline.`
        : 'Give script (or scriptPath).',
    )
  }
  let read: Awaited<ReturnType<typeof readScript>>
  try {
    read = await readScript(script)
  } catch (err) {
    return error((err as Error).message)
  }
  let resumeFrom: string | undefined
  if (input.resumeFromRunId !== undefined) {
    const id = /^wf_([0-9a-f]{8})$/.exec(input.resumeFromRunId)?.[1]
    const prior = id ? await ctx.tasks.get(ctx.sessionId, id) : undefined
    if (!prior || prior.kind !== 'workflow') return error(`No workflow run ${input.resumeFromRunId} in this session.`)
    if (prior.state === 'active') return error(`Workflow run ${input.resumeFromRunId} is still running: stop it first (TaskStop ${prior.id}).`)
    resumeFrom = prior.id
  }
  const { meta, body } = read
  const t = await ctx.tasks.create({
    sessionId: ctx.sessionId,
    kind: 'workflow',
    description: meta.description.slice(0, 500),
    spec: {
      name: meta.name,
      description: meta.description,
      ...(meta.phases ? { phases: meta.phases } : {}),
      body,
      ...(input.args === undefined ? {} : { args: input.args }),
      ...(resumeFrom ? { resumeFrom } : {}),
      scriptPath: '',
    },
    createdBy: ctx.owner,
    expiresAt: new Date(ctx.now().getTime() + WORKFLOW_EXPIRY_MS),
  })
  if (!t) return full()
  // Kept in the workspace, as Claude Code keeps it in its session dir, so it can be edited and run again.
  const scriptPath = workflowScriptPath(meta.name, t.id)
  let kept = false
  if (ctx.workspace) {
    try {
      await ctx.workspace.write(ws, workspaceRelative(scriptPath)!, Buffer.from(script, 'utf8'))
      await ctx.tasks.store.setSpec(t.id, { ...t.spec, scriptPath })
      kept = true
    } catch {
      // The run does not need the copy; without it there is no scriptPath to give.
    }
  }
  const runId = `wf_${t.id}`
  return {
    text:
      `Workflow launched in background. Task ID: ${t.id}\nSummary: ${meta.description}\n` +
      (kept
        ? `Script file: ${scriptPath}\n(Edit this file with workspace_edit or workspace_write and re-invoke Workflow with {scriptPath: "${scriptPath}"} to iterate without resending the script.)\n`
        : '') +
      `Run ID: ${runId}\n` +
      (kept
        ? `To resume after editing the script: Workflow({scriptPath: "${scriptPath}", resumeFromRunId: "${runId}"}) — completed agents return cached results.\n`
        : '') +
      `\nEach agent() runs as a chat of its own under this one (at most ${WORKFLOW_CONCURRENCY} at once and ${WORKFLOW_MAX_AGENTS} in all), ` +
      "spending from this session's budget; its tool calls ask the user as this session's do. You will be notified when it " +
      'completes: the session sends the result to you as a new message once this turn has ended and it is idle. The ScadBuddy ' +
      `panel shows its progress; TaskStop ${t.id} stops it.`,
  }
}
