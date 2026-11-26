import type { FeedItem } from './state'

// #782 — the feed's tool calls as the panel shows them: consecutive calls in one
// group, each with a status read off the feed (design
// docs/superpowers/specs/2026-10-08-friendly-tool-calls-design.md §3.3). Only tool
// items join a group: any other item, the approval and question cards included,
// ends it and keeps its own place.

export type ToolCall = Extract<FeedItem, { kind: 'tool' }>

export type FeedBlock =
  | { kind: 'item'; item: FeedItem }
  /** `id`: the first call's, so a group keeps its key as calls join it. */
  | { kind: 'tools'; id: string; calls: ToolCall[] }

export function feedBlocks(items: readonly FeedItem[]): FeedBlock[] {
  const blocks: FeedBlock[] = []
  for (const item of items) {
    const last = blocks.at(-1)
    if (item.kind !== 'tool') blocks.push({ kind: 'item', item })
    else if (last?.kind === 'tools') last.calls.push(item)
    else blocks.push({ kind: 'tools', id: item.id, calls: [item] })
  }
  return blocks
}

export type ToolStatus = 'running' | 'waiting_approval' | 'waiting_input' | 'done' | 'failed' | 'not_run' | 'stopped'

const LIVE: ReadonlySet<ToolStatus> = new Set(['running', 'waiting_approval', 'waiting_input'])

export function isLiveStatus(status: ToolStatus): boolean {
  return LIVE.has(status)
}

/**
 * How a call stands. An approval that names it and was not given says it was not run,
 * even when Claude Code went on to record an error result for it (#2149); one still
 * pending says it waits. Else its result says done or failed, unless the turn was
 * stopped while it ran (Claude Code then records a refusal as its result), which is
 * stopped. Else a question that names it says it waits on the user; else it runs.
 * `settled`: the session's turn has ended, so a call with nothing to show for it stopped.
 */
export function toolStatus(call: ToolCall, items: readonly FeedItem[], options: { settled?: boolean } = {}): ToolStatus {
  for (const item of items) {
    if (item.kind !== 'approval' || item.tool !== call.id) continue
    if (item.state === 'pending' || item.state === 'sent') {
      if (!call.result) return 'waiting_approval'
    } else if (item.state !== 'approved') return 'not_run'
  }
  if (call.result) {
    if (call.result.ok) return 'done'
    return stoppedBy(call, items) ? 'stopped' : 'failed'
  }
  for (const item of items) {
    if (item.kind === 'question' && item.tool === call.id && (item.state === 'pending' || item.state === 'sent')) {
      return 'waiting_input'
    }
  }
  // #7874: a turn placed again goes on without a result for this call. The call stopped with its worker.
  return options.settled || stoppedBy(call, items) === WORKER_RESTARTED ? 'stopped' : 'running'
}

/**
 * The agent's `error` code when a worker shutdown cut the turn (agent sessions/turnHost.ts
 * `WORKER_RESTARTED`, #7874). The workflow placed the turn again on another worker.
 */
export const WORKER_RESTARTED = 'worker_restarted'

/** The panel's words for a `worker_restarted` stop, on the call and in the feed. */
export const WORKER_RESTARTED_TEXT = 'The worker restarted; the turn continued on another worker.'

/** The error codes that stop a turn while a call runs: a Stop (#2149), or a worker shutdown (#7874). */
const STOP_CODES: ReadonlySet<string> = new Set(['interrupted', WORKER_RESTARTED])

/**
 * Why the turn stopped while `call` was still its last step (nothing the assistant or the
 * user said comes between them): the code of the agent's `interrupted` or `worker_restarted`
 * error. Undefined when the turn did not stop there.
 */
export function stoppedBy(call: ToolCall, items: readonly FeedItem[]): string | undefined {
  const at = items.indexOf(call)
  if (at < 0) return undefined
  // A call that started after this one's result came: this one had ended, so its failure is
  // its own, not the stop's (#7911). Unknown (no `resultAfter`, or it left the feed): as before.
  // The match is by id alone: `freshId` can give a later item the id of an error that
  // `dropErrorsAbout` removed, so `ended` can point too far down. Then the scan goes past later
  // calls it should stop at, and the call shows "Stopped" as before #7911, never a wrong failure
  // (#7920).
  const ended = call.resultAfter === undefined ? -1 : items.findIndex((i) => i.id === call.resultAfter)
  for (const [offset, item] of items.slice(at + 1).entries()) {
    if (item.kind === 'error' && item.code !== undefined && STOP_CODES.has(item.code)) return item.code
    if (item.kind === 'assistant' || item.kind === 'user') return undefined
    if (item.kind === 'tool' && ended >= 0 && at + 1 + offset > ended) return undefined
  }
  return undefined
}

/** The reason a stopped call's line shows, from `stoppedBy`'s code. Undefined when "Stopped" says enough (a Stop). */
export function stopReasonText(code: string | undefined): string | undefined {
  return code === WORKER_RESTARTED ? WORKER_RESTARTED_TEXT : undefined
}

/** A group's status: its first live call's; else failed, not run or stopped if any call was; else done. */
export function groupStatus(statuses: readonly ToolStatus[]): ToolStatus {
  const live = statuses.find(isLiveStatus)
  if (live) return live
  for (const status of ['failed', 'not_run', 'stopped'] as const) if (statuses.includes(status)) return status
  return 'done'
}

const STATUS_LABEL: Record<ToolStatus, string> = {
  running: 'Running',
  waiting_approval: 'Waiting for approval',
  waiting_input: 'Waiting for you',
  done: 'Done',
  failed: 'Failed',
  not_run: 'Not run',
  stopped: 'Stopped',
}

export function toolStatusLabel(status: ToolStatus): string {
  return STATUS_LABEL[status]
}
