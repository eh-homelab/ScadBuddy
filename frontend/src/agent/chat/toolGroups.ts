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
    return stoppedDuring(call, items) ? 'stopped' : 'failed'
  }
  for (const item of items) {
    if (item.kind === 'question' && item.tool === call.id && (item.state === 'pending' || item.state === 'sent')) {
      return 'waiting_input'
    }
  }
  return options.settled ? 'stopped' : 'running'
}

/**
 * Whether the turn was stopped (the agent's `interrupted` error) while `call` was still
 * its last step: nothing the assistant or the user said comes between them.
 */
function stoppedDuring(call: ToolCall, items: readonly FeedItem[]): boolean {
  const at = items.indexOf(call)
  if (at < 0) return false
  for (const item of items.slice(at + 1)) {
    if (item.kind === 'error' && item.code === 'interrupted') return true
    if (item.kind === 'assistant' || item.kind === 'user') return false
  }
  return false
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
