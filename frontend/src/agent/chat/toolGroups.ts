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
 * How a call stands: its result says done or failed; else the approval or question
 * that names it says whether it waits on a human or was never run; else it runs.
 * `settled`: the session's turn has ended, so a call with nothing to show for it stopped.
 */
export function toolStatus(call: ToolCall, items: readonly FeedItem[], options: { settled?: boolean } = {}): ToolStatus {
  if (call.result) return call.result.ok ? 'done' : 'failed'
  for (const item of items) {
    if (item.kind === 'approval' && item.tool === call.id) {
      if (item.state === 'pending' || item.state === 'sent') return 'waiting_approval'
      if (item.state !== 'approved') return 'not_run'
    }
    if (item.kind === 'question' && item.tool === call.id && (item.state === 'pending' || item.state === 'sent')) {
      return 'waiting_input'
    }
  }
  return options.settled ? 'stopped' : 'running'
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
