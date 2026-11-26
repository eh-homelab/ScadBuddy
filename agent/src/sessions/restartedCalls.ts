import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { TOOL_CALL_RESULT_TOOL } from '../temporal/toolCall.js'

// The calls a lost placement left in flight (#7874, unified runtime spec 2026-10-13 §5.5). A
// shutdown's cut drops what Claude Code writes after its interrupt (warm.ts `seal`), so the
// transcript can end with assistant tool_use blocks that have no result. A placement that was
// lost another way (its pod died, it was cut off) leaves the same. The restarted placement gives
// each such call its result in one user entry before the process resumes (turnHost.ts), so the
// model reads how each call ended, never "The user doesn't want to proceed with this tool use".

/** A tool_use of the transcript's last reply that has no tool_result. */
export type DanglingCall = { id: string; name: string }

/** How a dangling call ended, as its tool_result says it. */
export type CallEnd =
  /** A ToolCall workflow's own result (activityProjection.ts `callToolResult`), or its failure. */
  | { kind: 'result'; result: CallToolResult }
  /** A ToolCall still running once the wait for it ended. */
  | { kind: 'running' }
  /** A ToolCall that ended with no result: cancelled, terminated or timed out. */
  | { kind: 'ended'; status: string }
  /** Not a ToolCall: a Claude Code built-in, or a call in the turn's own process. */
  | { kind: 'none' }

type Block = { type?: unknown; id?: unknown; name?: unknown; tool_use_id?: unknown }

function blocksOf(entry: SessionStoreEntry): Block[] {
  const content = (entry.message as { content?: unknown } | undefined)?.content
  return Array.isArray(content) ? (content as Block[]) : []
}

/** An entry of the main conversation's chain: it has a uuid and a parent link, and is not a subagent's. */
function inChain(entry: SessionStoreEntry): boolean {
  return typeof entry.uuid === 'string' && 'parentUuid' in entry && entry.isSidechain !== true
}

/**
 * The calls of the main transcript's last reply that have no tool_result after it, and the entry
 * a new entry follows (the chain's last). Claude Code writes each block of a reply as an entry of
 * its own with the reply's API `message.id` (forkPoint.ts), and each result as a user entry after
 * them. Only the last reply: the Messages API takes a tool_result only right after its tool_use.
 */
export function danglingCalls(entries: readonly SessionStoreEntry[]): { calls: DanglingCall[]; leaf: SessionStoreEntry | undefined } {
  const chain = entries.filter(inChain)
  const leaf = chain.at(-1)
  let last = -1
  for (let i = chain.length - 1; i >= 0; i--) {
    if (chain[i]!.type === 'assistant') {
      last = i
      break
    }
  }
  if (last < 0) return { calls: [], leaf }
  const apiId = (chain[last]!.message as { id?: unknown } | undefined)?.id
  let first = last
  while (first > 0 && chain[first - 1]!.type === 'assistant' && apiId !== undefined && (chain[first - 1]!.message as { id?: unknown } | undefined)?.id === apiId) first--
  const answered = new Set<string>()
  for (const entry of chain.slice(last + 1)) {
    if (entry.type !== 'user') continue
    for (const b of blocksOf(entry)) if (b.type === 'tool_result' && typeof b.tool_use_id === 'string') answered.add(b.tool_use_id)
  }
  const calls: DanglingCall[] = []
  for (const entry of chain.slice(first, last + 1)) {
    for (const b of blocksOf(entry)) {
      if (b.type === 'tool_use' && typeof b.id === 'string' && !answered.has(b.id)) calls.push({ id: b.id, name: typeof b.name === 'string' ? b.name : 'unknown' })
    }
  }
  return { calls, leaf }
}

/** What a call that is not a ToolCall reads: it ran in the process the restart ended. */
export function restartedCallText(name: string): string {
  return `The agent service restarted while this ${name} call ran. The call did not return a result, and it may have run in part. Call it again if you still need it; first check what it changes, if it changes anything.`
}

/** What a ToolCall still running reads: it runs on, once, and its result can be read later. */
export function stillRunningText(name: string, id: string): string {
  return `The agent service restarted while this ${name} call ran. The call is still running, and it is not run again: do not call it again. Read its result later with the ${TOOL_CALL_RESULT_TOOL} tool, giving call id ${id}.`
}

/** What a ToolCall that ended with no result reads. */
export function endedText(name: string, status: string): string {
  return `The agent service restarted while this ${name} call ran, and the call then ended with no result (${status}). It may have run in part: check before you call it again.`
}

/** A tool_result block's content, as Claude Code gives the Messages API an MCP result. */
type ResultContent = { type: 'text'; text: string } | { type: 'image'; source: { type: 'base64'; media_type: string; data: string } }

/** One dangling call's tool_result block. */
export function toolResultBlock(call: DanglingCall, end: CallEnd): Record<string, unknown> {
  if (end.kind !== 'result') {
    const text = end.kind === 'running' ? stillRunningText(call.name, call.id) : end.kind === 'ended' ? endedText(call.name, end.status) : restartedCallText(call.name)
    return { type: 'tool_result', tool_use_id: call.id, content: [{ type: 'text', text }], is_error: true }
  }
  const content: ResultContent[] = end.result.content.map((c): ResultContent => {
    if (c.type === 'text') return { type: 'text', text: c.text }
    // The model boundary: an MCP result's image, which Claude Code itself keeps in the transcript and sends to the Messages API.
    if (c.type === 'image') return { type: 'image', source: { type: 'base64', media_type: c.mimeType, data: c.data } }
    return { type: 'text', text: `[${c.type}]` }
  })
  return { type: 'tool_result', tool_use_id: call.id, content, ...(end.result.isError ? { is_error: true } : {}) }
}

/**
 * The user entry that carries the results, after `leaf`, in the shape Claude Code writes a tool
 * result's entry in (test/fixtures/durable_conversation/entries.jsonl, which the bundled Claude Code
 * resumes from in test/durableImport.sdk.test.ts). `sessionId` is this session's: a fork's leaf can
 * be its parent's entry.
 */
export function resultsEntry(
  sessionId: string,
  leaf: SessionStoreEntry | undefined,
  results: readonly Record<string, unknown>[],
  uuid: string,
  now: Date,
): SessionStoreEntry {
  const kept = (key: string) => (leaf && leaf[key] !== undefined ? { [key]: leaf[key] } : {})
  return {
    type: 'user',
    uuid,
    parentUuid: leaf?.uuid ?? null,
    isSidechain: false,
    userType: 'external',
    sessionId,
    ...kept('cwd'),
    ...kept('version'),
    ...kept('gitBranch'),
    timestamp: now.toISOString(),
    message: { role: 'user', content: [...results] },
  }
}
