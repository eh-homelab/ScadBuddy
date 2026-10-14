import type { SDKMessage, SDKResultMessage, SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { context as otelContext } from '@opentelemetry/api'
import type { AuditLog } from '../audit/log.js'
import { TurnAuditor } from '../audit/turn.js'
import type { Credential } from '../credentials.js'
import { describeApiFailure, type FailureEvidence, type ProbeVerdict } from '../harness/credentialErrors.js'
import { type CredentialSource, runWithFallback } from '../harness/fallback.js'
import type { ApprovalGate, TierResolver } from '../harness/permissions.js'
import type { QuestionGate } from '../harness/questions.js'
import { redact } from '../secrets.js'
import { TurnTrace } from '../telemetry/turn.js'
import type { WaitForTab } from '../tools/registry.js'
import type { SessionBlobs } from './blobs.js'
import type { EventLog } from './eventLog.js'
import type { ClaimedSession, TurnOutcome, TurnPrincipal } from './manager.js'
import { event, type ServerEvent, type SessionStatus } from './protocol.js'
import { scrubForLog, SdkEventMapper, ShownCalls, type TitleResolver } from './sdkEvents.js'
import { type AssembledTurn, TurnAssemblyError, type TurnRunner } from './turnRunner.js'
import { UnpricedSpend } from './unpricedSpend.js'
import type { WarmEntry, WarmSessions, WarmTurn } from './warm.js'
import { continuationMemory, warmAttempts, type WarmTurnRun } from './warmTurn.js'

// One classic turn on the session's warm Claude Code, whoever runs it (unified runtime spec
// 2026-10-13 §5.2, §5.3): `SessionManager.runTurn` for a classic session, under its row lease,
// and `TurnHost.run` (turnHost.ts) for an AgentSession's `session_turn` activity. Both get the
// same assembly, warm process, fallback, events, audit and trace; what differs is around it:
// how the turn is claimed and stopped, which gates its parked calls go to (`TurnGates`), and
// how its end is written (`tallyTurn` gives both the cost, the turns and the closing events).

/** A turn that ended on a failed model request (fallback.ts `onRefused`). */
export type Refused = { evidence: FailureEvidence; probe?: ProbeVerdict['verdict'] }

/** What a turn's process needs besides the session: the manager's, shared by every turn on this replica. */
export type TurnCoreDeps = {
  runner: Pick<TurnRunner, 'assemble'>
  warm: WarmSessions
  events: Pick<EventLog, 'append' | 'committed'>
  blobs: Pick<SessionBlobs, 'put'>
  credentials: CredentialSource
  probe?: (credential: Credential, model: string | undefined) => Promise<ProbeVerdict>
  titleOf?: TitleResolver
  audit?: AuditLog
  stderr?: (text: string) => void
}

/** The gates a turn's parked calls go to: the classic services, or the session's workflow (Task 5.3). */
export type TurnGates = {
  approvalGate: ApprovalGate
  /** The approval a call was decided by, for its audit row. */
  approvalFor?: (toolUseId: string) => Promise<string | undefined>
  questionGate?: QuestionGate
  waitForTab?: WaitForTab
}

export type TurnGateContext = {
  /** The turn's first assembly. */
  turn: AssembledTurn
  secrets: () => readonly string[]
  trace: TurnTrace
  /** Logs a call's `tool.call` before it parks, once. */
  shown: (toolUseId: string, toolName: string, input: Record<string, unknown>) => Promise<void>
}

export type CoreTurnArgs = {
  session: ClaimedSession
  turnId: string
  principal: TurnPrincipal
  /** The user's words without the page context: what memory is recalled against. */
  userText: string
  /** The turn's own stop: an interrupt, a lost claim, a shutdown. */
  signal: AbortSignal
  /** What the process's `signalInterrupt` does (an attention request ending the turn). */
  interrupt: () => void
  gates: (ctx: TurnGateContext) => TurnGates
  /** The result is in (or the stream ended): the turn is finishing on its own. */
  onSettling?: () => void
  /** Each acquire of the warm process, before the prompt goes in. */
  onAcquired?: (acquired: WarmTurn) => void
  /** Each message of the turn's stream, as it comes (an AgentSession placement's running spend). */
  onMessage?: (message: SDKMessage) => void
  /** False once the turn may write nothing more (an AgentSession placement cut off): its events and images are dropped. */
  mayWrite?: () => boolean
  /**
   * How the turn's events are appended, when not `deps.events.append` (an AgentSession placement:
   * only while it holds its turn, checked in the append's transaction). False when refused.
   */
  appendEvents?: (events: ServerEvent[]) => Promise<boolean>
}

/** How the turn's stream ended. */
export type CoreTurnRun = {
  result?: SDKResultMessage
  /** The failed model request the result reports (fallback.ts `onRefused`). */
  refused?: Refused
  /** Why it ended without a result, when it was not stopped. */
  failure?: string
  secrets: readonly string[]
  /** What the attempts' results priced (warmTurn.ts `spentUsd`). */
  spentUsd: number
  /** What the request it was cut off in used, which no result prices (#991). */
  cutUsd: number
  /** The warm entries it ran on, one per attempt. */
  served: WarmEntry[]
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** A tool.result without its images, for when they could not be stored (#782). */
export function withoutImages(e: Extract<ServerEvent, { type: 'tool.result' }>): ServerEvent {
  const { images: _images, ...rest } = e
  return rest
}

function abortMessage(signal: AbortSignal): string {
  const reason: unknown = signal.reason
  return reason instanceof Error ? reason.message : 'the turn was interrupted'
}

export class CoreTurn {
  /** The turn's trace: a child of whatever started it, else a root. */
  readonly traced: TurnTrace
  readonly #deps: TurnCoreDeps
  readonly #args: CoreTurnArgs
  /** The assembly whose process serves the turn: the turn's own until the cache answers with a process spawned from another. */
  #turn: AssembledTurn | undefined
  readonly #served: WarmEntry[] = []
  /** The model requests of the turn's stream, priced at the table as they go (#991). */
  readonly #unpriced = new UnpricedSpend()

  constructor(deps: TurnCoreDeps, args: CoreTurnArgs) {
    this.#deps = deps
    this.#args = args
    const eventTierOf: TierResolver = (name, input) => this.#turn?.eventTierOf(name, input)
    this.traced = new TurnTrace({
      sessionId: args.session.id,
      turnId: args.turnId,
      parent: otelContext.active(),
      // Each tool's tier for its span (TierResolver takes an optional input).
      tierOf: (name) => eventTierOf(name) ?? 'outward',
      // What tallyTurn subtracts for the audit row, so the span's cost is this turn's (#2376).
      before: { costUsd: args.session.ownCostUsd, turns: args.session.turns },
    })
  }

  /** Runs the prompt on the session's warm process up to its result, logging what it shows. */
  async run(prompt: string | AsyncIterable<SDKUserMessage>): Promise<CoreTurnRun> {
    const { session, turnId, principal, signal } = this.#args
    const deps = this.#deps
    const id = session.id
    const shownTierOf: TierResolver = (name, input) => this.#turn?.shownTierOf(name, input)
    const mapper = new SdkEventMapper(id, shownTierOf, deps.titleOf)
    const shownCalls = new ShownCalls()
    const traced = this.traced
    /** Redacted from everything this turn writes to the durable event log: every assembly's secrets the turn ran with. */
    let secrets: readonly string[] = []
    // One audit row per tool call of this turn (#258, audit/turn.ts).
    const auditor = deps.audit
      ? new TurnAuditor(deps.audit, { sessionId: id, turnId, actor: session.owner, tierOf: shownTierOf, secrets: () => secrets })
      : undefined
    let result: SDKResultMessage | undefined
    let failure: string | undefined
    /** The model request the turn is cut off in, if it is: Claude Code never prices it (#991). */
    const unpriced = this.#unpriced
    /** The failed model request the turn ended on, as fallback.ts judged it (`onRefused`). */
    let refused: Refused | undefined
    /**
     * Claude Code's synthetic "API Error: …" messages, held until it is known
     * whether the turn ended on them (#1101): then the result's error event
     * says it, and they are dropped; otherwise they are shown as they came.
     */
    const heldErrors: SDKMessage[] = []
    const mayWrite = this.#args.mayWrite ?? (() => true)
    const log = async (events: ServerEvent[]) => {
      if (!mayWrite()) return
      if (events.length) {
        const scrubbed = events.map((e) => scrubForLog(e, secrets))
        if (this.#args.appendEvents) {
          if (!(await this.#args.appendEvents(scrubbed))) return
        } else await deps.events.append(id, scrubbed)
      }
      for (const e of events) traced.observe(e)
      if (auditor) for (const e of events) await auditor.observe(e)
    }
    const show = async (message: SDKMessage) => {
      await this.#turn?.observe(message)
      let events = mapper.map(message)
      // A result's images are stored before the event that names them is
      // logged (#782), so no watcher sees a name it cannot load. If they cannot
      // be, the event goes without them: the call is still shown.
      const images = mapper.takeImages()
      if (images.length && mayWrite()) {
        try {
          await deps.blobs.put(id, images)
        } catch (err) {
          deps.stderr?.(`session ${id}: could not store a tool result's images: ${describe(err)}\n`)
          events = events.map((e) => (e.type === 'tool.result' && e.images ? withoutImages(e) : e))
        }
      }
      const logged = log(events)
      shownCalls.logging(events, logged)
      await logged
    }
    const flushErrors = async () => {
      for (const m of heldErrors.splice(0)) await show(m)
    }
    /** The turn's attempts on the session's warm process (warmTurn.ts). */
    let attempts: WarmTurnRun | undefined
    /** Whether the attempt about to start continues an earlier one with CONTINUE_PROMPT (fallback.ts): no recall again (#1896). */
    let continuing = false
    try {
      const turn = await deps.runner.assemble(session, principal)
      this.#turn = turn
      secrets = turn.secrets
      const gates = this.#args.gates({
        turn,
        secrets: () => secrets,
        trace: traced,
        shown: (toolUseId, toolName, input) =>
          shownCalls.ensure(toolUseId, () => {
            const call = mapper.call(toolUseId, toolName, input)
            return call ? log([call]) : Promise.resolve()
          }),
      })
      const approvalGate = auditor ? auditor.gate(gates.approvalGate, gates.approvalFor) : gates.approvalGate
      // What the process's callbacks read as the turn (turnSlot.ts): set on
      // the slot of the assembly its process was spawned from, at each
      // acquire, before the prompt goes in. On a hit that is not this turn's
      // own assembly: its secrets, tiers and hooks are the turn's from then on.
      const onAcquired = (acquired: WarmTurn) => {
        const { entry } = acquired
        this.#turn = entry.turn
        secrets = [...new Set([...secrets, ...entry.turn.secrets])]
        this.#served.push(entry)
        const memory = entry.turn.memory?.(this.#args.userText, turnId)
        entry.turn.slot.set({
          turnId,
          principal,
          approvalGate,
          ...(gates.questionGate ? { questionGate: gates.questionGate } : {}),
          ...(gates.waitForTab ? { waitForTab: gates.waitForTab } : {}),
          ...(auditor ? { auditor } : {}),
          trace: traced,
          http: { turnId, signal },
          ...(memory ? { memory: continuing ? continuationMemory(memory) : memory } : {}),
          signalInterrupt: () => this.#args.interrupt(),
        })
        this.#args.onAcquired?.(acquired)
      }
      const warm = warmAttempts({
        warm: deps.warm,
        sessionId: id,
        first: turn,
        assemble: () => deps.runner.assemble(session, principal),
        stop: signal,
        acquired: onAcquired,
        trace: traced,
      })
      attempts = warm
      const resume = turn.run.resume !== undefined
      const stream = runWithFallback(
        { ...turn.run, prompt, signal },
        {
          candidates: turn.candidates,
          report: deps.credentials.reporter({ sessionId: id, turnId }),
          // Each attempt runs on the session's warm process, acquired inside the turn's span.
          run: (attempt) => otelContext.with(traced.context(), () => warm.run(attempt)),
          ...(deps.probe ? { probe: deps.probe } : {}),
          // A resumed query's total includes what the session spent before
          // (fallback.ts `Spend`), as far as Claude Code priced it (#991).
          ...(resume ? { priorCostUsd: session.ownCostUsd - session.unpricedCostUsd } : {}),
          // The warm process is the session's own: a turn it serves after
          // its first sees no init message, and a failed attempt still
          // continues there (fallback.ts `session`).
          session: id,
          onRefused: (evidence, judged) => {
            refused = { evidence, ...judged }
          },
          onAttempt: () => {
            continuing = true
            unpriced.newAttempt()
          },
        },
      )
      for await (const message of stream) {
        unpriced.observe(message)
        this.#args.onMessage?.(message)
        if (message.type === 'assistant' && message.error) {
          heldErrors.push(message)
          continue
        }
        if (message.type === 'result') {
          result = message
          this.#args.onSettling?.()
          // onRefused runs before fallback.ts yields what it held.
          if (refused) heldErrors.length = 0
        }
        await flushErrors()
        await show(message)
      }
      await flushErrors()
    } catch (err) {
      // An assembly that failed released what it held, and says which secrets it had read.
      if (err instanceof TurnAssemblyError) secrets = err.secrets
      const cause = err instanceof TurnAssemblyError ? err.cause : err
      if (!refused) await flushErrors().catch(() => {})
      // For an error result the SDK yields the result and then throws
      // ("Claude Code returned an error result", test/run.test.ts); the
      // result is what counts then.
      if (!result && !signal.aborted) {
        failure = redact(describe(cause), secrets)
        traced.fail(cause)
      }
    } finally {
      // An assembly the cache never took (the turn ended before an acquire) is still ours to release.
      attempts?.unused()?.release()
      this.#args.onSettling?.()
      await auditor?.finish(signal.aborted ? abortMessage(signal) : (failure ?? 'the turn ended first'))
    }
    return {
      ...(result ? { result } : {}),
      ...(refused ? { refused } : {}),
      ...(failure !== undefined ? { failure } : {}),
      secrets,
      spentUsd: attempts?.spentUsd() ?? 0,
      cutUsd: unpriced.usd(),
      served: [...this.#served],
    }
  }

  /**
   * What the turn's model requests have cost so far at the table's prices, finished or still
   * open: an AgentSession placement's running spend (turnHost.ts), written as it goes so a
   * placement that dies before its result still counts against the budget.
   */
  runningUsd(): number {
    return this.#unpriced.completedUsd() + this.#unpriced.usd()
  }

  /**
   * The turn has finished, its events written: its span ends, and a hook its
   * process fires from now on finds no turn (turnSlot.ts). Only this turn's
   * slot: the next one may already have set it for itself.
   */
  end(outcome: TurnOutcome, result: SDKResultMessage | undefined): void {
    this.traced.finish(outcome, result)
    for (const entry of this.#served) {
      if (entry.turn.slot.current?.turnId === this.#args.turnId) entry.turn.slot.clear()
    }
  }
}

/**
 * A turn's closing events (`tallyTurn`'s `tail`) worded once the row says what the lineage has
 * spent and what its budget is now (#823): the meter's numbers, and the budget's message.
 */
export function wordTail(tail: readonly ServerEvent[], now: { spentUsd: number; budgetUsd: number; turns?: number }): ServerEvent[] {
  const usd = (amount: number): string => `$${amount.toFixed(2)}`
  return tail.map((e): ServerEvent => {
    if (e.type === 'session.result') return { ...e, costUsd: now.spentUsd, budgetUsd: now.budgetUsd, ...(now.turns !== undefined ? { turns: now.turns } : {}) }
    if (e.type === 'error' && e.code === 'error_max_budget_usd') {
      return { ...e, message: `this chat used its ${usd(now.budgetUsd)} budget (${usd(now.spentUsd)} spent)` }
    }
    return e
  })
}

/** What a turn's end comes to: the session's cost and turns, its status, its outcome and its closing events. */
export type TurnTally = {
  status: SessionStatus
  outcome: Exclude<TurnOutcome, { kind: 'lost_claim' }>
  /** The session row's own cost and turns after the turn. */
  costUsd: number
  turns: number
  /** `session.result` (its budget filled in later), the error if any, and the final `session.status`. */
  tail: ServerEvent[]
}

/**
 * The end of a turn as a classic turn writes it (manager.ts `finish`), from how its stream
 * ended. `stopped` is why it was aborted, when it was; `interrupted` the `interrupted`
 * error's message then.
 */
export type TallyInput = {
  stopped: string | undefined
  interrupted: string | undefined
  result: SDKResultMessage | undefined
  refused: Refused | undefined
  failure: string | undefined
  secrets: readonly string[]
  /** What the turn's attempts spent as their results priced it (warm.ts `turnCostUsd`). */
  turnUsd: number
  /** What the request the turn was cut off in used, which no result prices (#991, unpricedSpend.ts). */
  cutUsd: number
}

export function tallyTurn(session: Pick<ClaimedSession, 'id' | 'ownCostUsd' | 'turns'>, end: TallyInput): TurnTally {
  const id = session.id
  const { stopped, interrupted, result, refused, failure, secrets, turnUsd, cutUsd } = end
  let status: SessionStatus
  let outcome: TurnTally['outcome']
  const tail: ServerEvent[] = []
  // The session's own row, not the lineage's pool (#823): only its own turns ran on its process.
  let costUsd = session.ownCostUsd
  let turns = session.turns
  if (result && stopped !== undefined) {
    // Stopped, and Claude Code still reported a result: the interrupt's
    // `error_during_execution` (harness/run.ts stopFirst), or a result that
    // raced the stop. The turn reads interrupted either way (#1168); what it
    // spent before it stopped is still counted, the cut-off request too.
    costUsd = session.ownCostUsd + turnUsd + cutUsd
    turns = session.turns + result.num_turns
    status = 'idle'
    tail.push(event({ type: 'session.result', sessionId: id, costUsd, turns }))
    tail.push(event({ type: 'error', sessionId: id, code: 'interrupted', message: interrupted ?? 'the turn was interrupted' }))
    outcome = { kind: 'interrupted' }
  } else if (result) {
    costUsd = session.ownCostUsd + turnUsd
    if (refused) {
      // Claude Code reports a refused request as a `success` result with
      // `is_error` set, and its "API Error: …" text as a synthetic reply
      // (#1101). Redacted here too: the outcome reaches MCP callers without
      // scrubForLog. The refused request is not a turn; any before it are.
      const refusal = redact(describeApiFailure(refused.evidence, refused), secrets)
      // `num_turns` counts the refused request (fallback.ts `onRefused`).
      turns = session.turns + Math.max(result.num_turns - 1, 0)
      status = 'failed'
      tail.push(event({ type: 'session.result', sessionId: id, costUsd, turns }))
      tail.push(event({ type: 'error', sessionId: id, code: 'api_error', message: refusal }))
      outcome = { kind: 'failed', message: refusal }
    } else {
      turns = session.turns + result.num_turns
      status = result.subtype === 'error_during_execution' ? 'failed' : 'idle'
      // Its budget is filled in from the row once the claim is released.
      tail.push(event({ type: 'session.result', sessionId: id, costUsd, turns }))
      if (result.subtype === 'error_max_budget_usd') {
        // The SDK's own text names this turn's share of the budget as an
        // unrounded float; the session's budget, in cents, is what the user
        // set. Worded once the row says what the budget is now.
        tail.push(event({ type: 'error', sessionId: id, code: result.subtype, message: 'the chat used its budget' }))
      } else if (result.subtype !== 'success') {
        const detail = 'errors' in result && result.errors.length ? `: ${result.errors.join('; ')}` : ''
        tail.push(event({ type: 'error', sessionId: id, code: result.subtype, message: `the turn stopped (${result.subtype})${detail}` }))
      }
      outcome = { kind: 'result', subtype: result.subtype, costUsd, turns }
    }
  } else if (stopped !== undefined) {
    status = 'idle'
    if (cutUsd > 0) {
      // No result, but the request it was cut off in was billed: the meter shows it.
      costUsd = session.ownCostUsd + cutUsd
      tail.push(event({ type: 'session.result', sessionId: id, costUsd, turns }))
    }
    tail.push(event({ type: 'error', sessionId: id, code: 'interrupted', message: interrupted ?? 'the turn was interrupted' }))
    outcome = { kind: 'interrupted' }
  } else {
    status = 'failed'
    if (cutUsd > 0) {
      costUsd = session.ownCostUsd + cutUsd
      tail.push(event({ type: 'session.result', sessionId: id, costUsd, turns }))
    }
    const message = failure ?? 'the turn ended without a result'
    tail.push(event({ type: 'error', sessionId: id, code: 'turn_failed', message }))
    outcome = { kind: 'failed', message }
  }
  tail.push(event({ type: 'session.status', sessionId: id, status }))
  return { status, outcome, costUsd, turns, tail }
}
