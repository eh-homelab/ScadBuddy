import path from 'node:path'
import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { Context } from '@temporalio/activity'
import type { AuditSink } from '../audit/log.js'
import {
  createMemoryHooks,
  HindsightClient,
  hindsightTarget,
  type MemoryHooks,
  type MemoryOptions,
  type SettingsReader,
  turnRecallMaxQueryChars,
} from '../memory/hindsight.js'
import type { PluginsForRun } from '../plugins/forwarder.js'
import type { EventLog, LoggedEvent } from '../sessions/eventLog.js'
import { recordMemoryActivity } from '../sessions/manager.js'
import type { Owner } from '../sessions/protocol.js'
import { durableSession, type SessionOwners } from './toolActivities.js'

// Automatic Hindsight memory in a durable session (#2320, superseding #825): what a
// classic turn's memory hooks do (memory/hindsight.ts `createMemoryHooks`, built by
// sessions/manager.ts `memoryHooks`), run by the agent as two activities the workflow
// calls, around the turn, since a durable turn's Claude Code runs in agent-durable with
// no hooks of ScadBuddy's. The same hooks are built and called here, so the recall
// query, the injected block, the retained document (`conversation:<session>`, upserted
// each turn) and the audit rows and `memory` events are the classic ones.
//
//   memory_recall  at a turn's start: the `<hindsight_memories>` block the classic
//                  UserPromptSubmit hook adds, recalled against the user's words (the
//                  turn's `user.turn` event, without the page context); the workflow
//                  appends it to the prompt. None when memory is off or nothing matched.
//   memory_retain  at a turn's end, before finish_turn: the classic Stop hook over the
//                  session's transcript, rebuilt from its event log (`transcriptOf`).
//
// Neither ever fails: a failure is logged and recorded in the session as classic records
// it, and the activity returns as if there were no memory. Only the recalled block crosses
// the history (sealed by the session's payload codec); never a secret or a bank address.

export const MEMORY_RECALL_ACTIVITY = 'memory_recall'
export const MEMORY_RETAIN_ACTIVITY = 'memory_retain'

export type MemoryArgs = { turn_id: string }
export type MemoryRecalled = { context: string | null }

export type MemoryActivityDeps = {
  sessions: SessionOwners
  events: Pick<EventLog, 'append' | 'read' | 'userTurnText'>
  /** The enabled remote plugins, among them `hindsight` (main.ts `remotePlugins`). */
  remotePlugins: () => Promise<PluginsForRun>
  /** Every usable credential's secret, redacted as a classic turn redacts them. */
  credentialSecrets?: (() => Promise<readonly string[]>) | undefined
  audit?: AuditSink | undefined
  /** ai_settings, for the recall query's cap, read at each turn as a classic turn reads it. */
  settings?: SettingsReader | undefined
  /** The hook knobs a classic turn takes (SessionManagerDeps.memory); false: memory off. */
  memory?: MemoryOptions | undefined
  log?: (line: string) => void
}

/** Every event of a session, oldest first. */
async function allEvents(events: MemoryActivityDeps['events'], session: string): Promise<LoggedEvent[]> {
  const out: LoggedEvent[] = []
  for (;;) {
    const page = await events.read(session, out.at(-1)?.seq ?? 0)
    if (page.length === 0) return out
    out.push(...page)
  }
}

/**
 * The session's conversation as Claude Code transcript lines (what the classic Stop hook
 * reads): each `user.turn`, each assistant message's text, and each of the main agent's
 * tool calls. A subagent's calls (`parent`) are its sidechain, left out as there, and so is a
 * refused credential's notice (`notice`, #3703), as the classic transcript leaves out
 * Claude Code's synthetic error message (memory/hindsight.ts, #3701).
 */
export function transcriptOf(events: readonly LoggedEvent[]): SessionStoreEntry[] {
  const lines: SessionStoreEntry[] = []
  const texts = new Map<string, { content: { type: string; text?: string; name?: string; input?: unknown }[] }>()
  for (const { event } of events) {
    if (event.type === 'user.turn') {
      lines.push({ type: 'user', message: { content: event.text } })
    } else if (event.type === 'assistant.text.delta' && event.notice !== true) {
      let message = texts.get(event.messageId)
      if (!message) {
        message = { content: [{ type: 'text', text: '' }] }
        texts.set(event.messageId, message)
        lines.push({ type: 'assistant', message })
      }
      message.content[0]!.text += event.delta
    } else if (event.type === 'tool.call' && event.parent === undefined) {
      // Text the message goes on with after the call is a new line after it (#2419).
      // Clearing them all is right because the main agent streams one message at a
      // time: only the one the call belongs to is open. That holds because the durable
      // log's text comes from agent-durable's `translate` (session/events.py), which
      // writes each text as a whole message of its own; a change there that streams two
      // at once must revisit this (#3362). Subagents' calls carry a `parent` and never
      // reach here.
      texts.clear()
      lines.push({ type: 'assistant', message: { content: [{ type: 'tool_use', name: event.name, input: event.input }] } })
    }
  }
  return lines
}

export function memoryActivities(deps: MemoryActivityDeps) {
  const log = deps.log ?? ((line: string) => process.stderr.write(line))

  /** The turn's hooks, as a classic turn builds them, or undefined when memory is off. */
  async function hooksFor(
    session: { id: string; owner: Owner },
    turnId: string,
    recallQuery: string,
  ): Promise<{ hooks: MemoryHooks; release: () => void } | undefined> {
    if (deps.memory === false) return undefined
    const forwarded = await deps.remotePlugins()
    const release = () => void forwarded.release()
    if (!forwarded.hindsight) {
      release()
      return undefined
    }
    const secrets = [...forwarded.secrets, ...(deps.credentialSecrets ? await deps.credentialSecrets() : [])]
    let client: HindsightClient
    try {
      client = new HindsightClient(hindsightTarget(forwarded.hindsight))
    } catch (err) {
      log(`hindsight: automatic memory is off for this turn: ${(err as Error).message}\n`)
      release()
      return undefined
    }
    const config = deps.memory
    const fixed = config?.hookConfig?.recallQuery
    const recallMaxQueryChars = await turnRecallMaxQueryChars(deps.settings, config?.hookConfig)
    const hooks = createMemoryHooks({
      ...config,
      hookConfig: { ...config?.hookConfig, recallQuery: fixed && fixed !== '$prompt' ? fixed : recallQuery, recallMaxQueryChars },
      client,
      secrets,
      log,
      // The Stop hook's transcript: the session's event log (its file never exists here).
      sessionStore: { load: async () => transcriptOf(await allEvents(deps.events, session.id)) },
      onActivity: (activity) => recordMemoryActivity({ audit: deps.audit, events: deps.events }, session, turnId, activity, secrets),
    })
    return { hooks, release }
  }

  /** Runs one of the hooks' callbacks with the input Claude Code would give it. */
  async function call(hooks: MemoryHooks, event: 'UserPromptSubmit' | 'Stop', input: Record<string, unknown>) {
    const outputs: unknown[] = []
    const signal = Context.current().cancellationSignal
    for (const matcher of hooks.hooks[event] ?? []) {
      for (const hook of matcher.hooks) {
        outputs.push(await hook({ hook_event_name: event, ...input } as never, undefined, { signal }))
      }
    }
    return outputs
  }

  return {
    async [MEMORY_RECALL_ACTIVITY]({ turn_id }: MemoryArgs): Promise<MemoryRecalled> {
      try {
        const { session, owner } = await durableSession(MEMORY_RECALL_ACTIVITY, deps.sessions)
        const prompt = await deps.events.userTurnText(session, turn_id)
        if (!prompt) return { context: null }
        const built = await hooksFor({ id: session, owner }, turn_id, prompt)
        if (!built) return { context: null }
        try {
          const outputs = await call(built.hooks, 'UserPromptSubmit', { prompt, session_id: session, transcript_path: '', cwd: '' })
          await built.hooks.settled()
          for (const output of outputs) {
            const extra = (output as { hookSpecificOutput?: { additionalContext?: string } }).hookSpecificOutput?.additionalContext
            if (extra) return { context: extra }
          }
          return { context: null }
        } finally {
          built.release()
        }
      } catch (err) {
        log(`hindsight: recall for a durable turn failed: ${(err as Error).message}\n`)
        return { context: null }
      }
    },

    async [MEMORY_RETAIN_ACTIVITY]({ turn_id }: MemoryArgs): Promise<void> {
      try {
        const { session, owner } = await durableSession(MEMORY_RETAIN_ACTIVITY, deps.sessions)
        const built = await hooksFor({ id: session, owner }, turn_id, '')
        if (!built) return
        try {
          // A path that never exists, so the hook reads the session store (the event log).
          const transcript = path.join('/nonexistent', `durable-${session}.jsonl`)
          await call(built.hooks, 'Stop', { session_id: session, transcript_path: transcript, cwd: '', stop_hook_active: false })
          await built.hooks.settled()
        } finally {
          built.release()
        }
      } catch (err) {
        log(`hindsight: retain for a durable turn failed: ${(err as Error).message}\n`)
      }
    },
  }
}
