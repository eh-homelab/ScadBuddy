// agent/src/telemetry/turn.ts
import type { HookCallbackMatcher, HookEvent, SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { type Attributes, type Context, type Link, ROOT_CONTEXT, type Span, type SpanContext, SpanStatusCode, trace } from '@opentelemetry/api'
import type { ApprovalRecord, GateTrace, ParkTrace } from '../approvals/service.js'
import type { RiskTier } from '../harness/permissions.js'
import type { TurnOutcome } from '../sessions/manager.js'
import type { ServerEvent } from '../sessions/protocol.js'
import { bindToolContext, recordFailure, spanContextFrom, tracer, traceparentOf, unbindToolContext } from './trace.js'

// One chat turn's trace (spec 2026-10-01 §5.4). The turn is a sequence of
// SEGMENTS: `agent.turn` (segment 0), then an `agent.turn.resume` (1, 2, …)
// after each round of approvals. A span is exported only when it ends, so an
// outward call that parks ends its own `agent.tool/<name>` span and the open
// segment AT ONCE (outcome `parked`): a parked turn shows up in Tempo as soon
// as it parks, never after a 24-hour wait. The parked tool span's context goes
// on the ai_approvals row (ParkTrace.traceparent); the human's decision is its
// own trace (`agent.approval`, approvals/service.ts) linking back to it.
//
//   - Several calls of one segment may park (parallel tool use). The segment
//     ends once, at the first park; a later park ends only its own tool span,
//     and a call that did not park keeps its span open until it finishes.
//   - The next segment begins when the segment's LAST parked call is decided
//     (the harness continues only then): a child of that decision, linked to
//     the segment's other decisions. Names never nest (`.resume.resume`);
//     every segment carries `scadbuddy.turn_id` and `scadbuddy.segment`.
//   - A call belongs to the segment its span started in, and counts as
//     undecided there from park() on, before its row exists: a sibling decided
//     while this call's row is still being written does not open the resume
//     early, and this call's parked() ends its own segment, not the resume.
//     A call that parks only after its segment's parks were all decided (Claude
//     Code runs outward calls one at a time, so a message's second call parks
//     after the first ran) parks in the segment then open, and ends it.
//   - An approved call's execution is a new `agent.tool/<name>` span: under
//     the new segment when its decision opened one, else under its own
//     decision (a root linked to its parked span when the decision has no
//     trace context). Denied and expired calls run nothing.
//
// Tool spans start at whichever comes first of the PreToolUse hook, the
// mapped `tool.call` event and a park, and end at PostToolUse(Failure) or the
// `tool.result` event, whichever comes first. Each open tool span's context is
// bound under its tool_use id (telemetry/trace.ts), where the in-process tools
// find it. Nothing a tool is given or returns is recorded (§6).

export const TURN_SPAN = 'agent.turn'
export const RESUME_SPAN = 'agent.turn.resume'

export function toolSpanName(tool: string): string {
  return `agent.tool/${tool}`
}

export type TurnTraceOptions = {
  sessionId: string
  turnId: string
  /** The context the turn starts in: a browser frame's traceparent, an MCP call, a decision (an orphan's resume), or none. */
  parent: Context
  /** Each tool's tier, for its span. */
  tierOf: (toolName: string) => RiskTier
}

type Segment = { span: Span; index: number; ended: boolean; undecided: number; decisions: SpanContext[]; tools: number }
type Call = { name: string; span: Span; ended: boolean; segment: Segment; parkedIn?: Segment }

function outcomeOf(outcome: TurnOutcome): string {
  return outcome.kind === 'result' ? outcome.subtype : outcome.kind
}

/** The outcome's cost and model turns, and the result's token counts: never its text. */
function usageOf(outcome: TurnOutcome, result: SDKResultMessage | undefined): Attributes {
  const out: Attributes = {}
  if (outcome.kind === 'result') {
    out['scadbuddy.cost_usd'] = outcome.costUsd
    out['scadbuddy.turns'] = outcome.turns
  }
  const usage = result?.usage as Partial<Record<'input_tokens' | 'output_tokens', number>> | undefined
  if (typeof usage?.input_tokens === 'number') out['scadbuddy.input_tokens'] = usage.input_tokens
  if (typeof usage?.output_tokens === 'number') out['scadbuddy.output_tokens'] = usage.output_tokens
  return out
}

export class TurnTrace implements GateTrace {
  readonly #sessionId: string
  readonly #turnId: string
  readonly #tierOf: (toolName: string) => RiskTier
  readonly #calls = new Map<string, Call>()
  #segment: Segment
  /** What fail() was given; recorded only if the turn's outcome is `failed`. */
  #failure: { err: unknown } | undefined
  /** Set by finish(): Claude Code can still run briefly after an abort, and a wait can resolve late; nothing opens then. */
  #finished = false

  constructor(options: TurnTraceOptions) {
    this.#sessionId = options.sessionId
    this.#turnId = options.turnId
    this.#tierOf = options.tierOf
    this.#segment = this.#open(TURN_SPAN, 0, options.parent, [])
  }

  #ids(): Attributes {
    return { 'scadbuddy.session_id': this.#sessionId, 'scadbuddy.turn_id': this.#turnId }
  }

  #open(name: string, index: number, parent: Context, links: Link[]): Segment {
    const span = tracer().startSpan(name, { attributes: { ...this.#ids(), 'scadbuddy.segment': index }, links }, parent)
    return { span, index, ended: false, undecided: 0, decisions: [], tools: 0 }
  }

  #endSegment(segment: Segment, attributes: Attributes): void {
    if (segment.ended) return
    segment.span.setAttributes({ ...attributes, 'scadbuddy.tool_calls': segment.tools })
    segment.span.end()
    segment.ended = true
  }

  /** The open segment's context: what the turn's own work runs under. */
  context(): Context {
    return trace.setSpan(ROOT_CONTEXT, this.#segment.span)
  }

  #startTool(toolUseId: string, name: string, parent: Context, attributes: Attributes = {}, links: Link[] = []): Span {
    const span = tracer().startSpan(
      toolSpanName(name),
      {
        attributes: {
          ...this.#ids(),
          'scadbuddy.tool': name,
          'scadbuddy.tier': this.#tierOf(name),
          'scadbuddy.tool_use_id': toolUseId,
          ...attributes,
        },
        links,
      },
      parent,
    )
    bindToolContext(toolUseId, trace.setSpan(ROOT_CONTEXT, span))
    return span
  }

  #call(toolUseId: string, name: string): Call {
    const known = this.#calls.get(toolUseId)
    if (known) return known
    const segment = this.#segment
    segment.tools += 1
    const span = this.#startTool(toolUseId, name, trace.setSpan(ROOT_CONTEXT, segment.span))
    const call: Call = { name, ended: false, span, segment }
    this.#calls.set(toolUseId, call)
    return call
  }

  #endCall(toolUseId: string, call: Call, outcome: string, attributes: Attributes = {}): void {
    call.span.setAttributes({ 'scadbuddy.outcome': outcome, ...attributes })
    if (outcome === 'error') call.span.setStatus({ code: SpanStatusCode.ERROR })
    call.span.end()
    call.ended = true
    unbindToolContext(toolUseId)
  }

  toolStarted(toolUseId: string, name: string): void {
    if (!this.#finished) this.#call(toolUseId, name)
  }

  toolEnded(toolUseId: string, ok: boolean): void {
    const call = this.#calls.get(toolUseId)
    if (call && !call.ended) this.#endCall(toolUseId, call, ok ? 'ok' : 'error')
  }

  /** The turn's mapped panel events (sessions/sdkEvents.ts): a backstop for the hooks. */
  observe(e: ServerEvent): void {
    if (e.type === 'tool.call') this.toolStarted(e.id, e.name)
    else if (e.type === 'tool.result') this.toolEnded(e.id, e.ok)
  }

  /** SDK callback hooks (harness/run.ts HarnessRun.traceHooks): every tool's start and end. */
  hooks(): Partial<Record<HookEvent, HookCallbackMatcher[]>> {
    return {
      PreToolUse: [
        {
          hooks: [
            (input) => {
              if (input.hook_event_name === 'PreToolUse') this.toolStarted(input.tool_use_id, input.tool_name)
              return Promise.resolve({})
            },
          ],
        },
      ],
      PostToolUse: [
        {
          hooks: [
            (input) => {
              if (input.hook_event_name === 'PostToolUse') this.toolEnded(input.tool_use_id, true)
              return Promise.resolve({})
            },
          ],
        },
      ],
      PostToolUseFailure: [
        {
          hooks: [
            (input) => {
              if (input.hook_event_name === 'PostToolUseFailure') this.toolEnded(input.tool_use_id, false)
              return Promise.resolve({})
            },
          ],
        },
      ],
    }
  }

  park(toolUseId: string, toolName: string): ParkTrace {
    if (this.#finished) return { traceparent: undefined, parked: () => {}, decided: () => {}, abandoned: () => {} }
    const call = this.#call(toolUseId, toolName)
    // Undecided from now on, in the segment the call started in (see the header),
    // unless every park there was decided and a later segment opened: then the
    // call parks in the open segment, and its parked() ends that one.
    let segment = call.parkedIn ?? call.segment
    if (!call.parkedIn) {
      if (segment !== this.#segment && segment.undecided === 0) segment = this.#segment
      segment.undecided += 1
      call.parkedIn = segment
    }
    const parkedSpan = call.span.spanContext()
    let parked = false
    return {
      traceparent: traceparentOf(call.span),
      parked: (approvalId) => {
        if (parked || this.#finished) return
        parked = true
        if (!call.ended) this.#endCall(toolUseId, call, 'parked', { 'scadbuddy.approval_id': approvalId })
        this.#endSegment(segment, { 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': approvalId })
      },
      decided: (approval, runs) => this.#decided(toolUseId, call, parkedSpan, approval, runs),
      abandoned: () => this.#abandoned(call),
    }
  }

  /**
   * The call's row was never written, so no decision will come for it. Its
   * count is undone; if its siblings were all decided meanwhile, the resume
   * they were waiting on opens now, under the last of their decisions.
   */
  #abandoned(call: Call): void {
    const segment = call.parkedIn
    if (!segment || this.#finished) return
    call.parkedIn = undefined
    segment.undecided -= 1
    if (segment.undecided > 0 || segment !== this.#segment || !segment.ended) return
    const last = segment.decisions.at(-1)
    this.#resume(segment, last, last ? trace.setSpanContext(ROOT_CONTEXT, last) : ROOT_CONTEXT)
  }

  /** Opens the segment after `segment`: a child of `decision`, linked to the segment's other decisions. */
  #resume(segment: Segment, decision: SpanContext | undefined, decisionContext: Context): void {
    const others = segment.decisions.filter((d) => d !== decision).map((context) => ({ context }))
    this.#segment = this.#open(RESUME_SPAN, segment.index + 1, decisionContext, others)
  }

  #decided(
    toolUseId: string,
    call: Call,
    parkedSpan: SpanContext,
    approval: Pick<ApprovalRecord, 'id' | 'decision' | 'decisionTraceparent'>,
    runs: boolean,
  ): void {
    const segment = call.parkedIn
    if (!segment || this.#finished) return
    call.parkedIn = undefined
    segment.undecided -= 1
    const decision = spanContextFrom(approval.decisionTraceparent)
    if (decision) segment.decisions.push(decision)
    const decisionContext = decision ? trace.setSpanContext(ROOT_CONTEXT, decision) : ROOT_CONTEXT
    let parent = decisionContext
    let links: Link[] = []
    if (segment.undecided === 0 && segment === this.#segment) {
      this.#resume(segment, decision, decisionContext)
      parent = this.context()
    }
    if (!runs) return
    if (parent !== decisionContext) this.#segment.tools += 1
    // Run under its own decision with no trace context: a root, so link it to where it parked.
    else if (!decision) links = [{ context: parkedSpan }]
    call.span = this.#startTool(toolUseId, call.name, parent, { 'scadbuddy.approval_id': approval.id }, links)
    call.ended = false
  }

  /** The turn failed with `err`: its class goes on the open segment if the outcome is `failed`, never its message. */
  fail(err: unknown): void {
    this.#failure ??= { err }
  }

  /**
   * Ends whatever is still open: tool spans as `unfinished`, the open segment
   * with the turn's outcome, cost and tokens. Only a `failed` outcome is an
   * ERROR: an interrupt, a lost claim or a result subtype such as
   * `error_max_turns` is the turn ending, not the agent failing.
   */
  finish(outcome: TurnOutcome, result?: SDKResultMessage): void {
    if (this.#finished) return
    this.#finished = true
    for (const [toolUseId, call] of this.#calls) if (!call.ended) this.#endCall(toolUseId, call, 'unfinished')
    const segment = this.#segment
    if (!segment.ended && outcome.kind === 'failed') {
      if (this.#failure) recordFailure(segment.span, this.#failure.err)
      else segment.span.setStatus({ code: SpanStatusCode.ERROR })
    }
    this.#endSegment(segment, { 'scadbuddy.outcome': outcomeOf(outcome), ...usageOf(outcome, result) })
  }
}
