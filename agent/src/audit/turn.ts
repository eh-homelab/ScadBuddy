import type { ApprovalGate, ApprovalVerdict, RiskTier, TierResolver } from '../harness/permissions.js'
import type { ServerEvent } from '../sessions/protocol.js'
import { unwrapUntrusted } from '../safety/untrusted.js'
import { type AuditActor, type AuditLog, type AuditOutcome, safeDetail } from './log.js'

// The harness half of the audit log (#258): one `tool_call` row per tool call
// a session turn makes. It watches the turn's panel events (sessions/
// sdkEvents.ts: `tool.call` when the model asks, `tool.result` when the
// result goes back) and the approval gate's verdicts, so it sees every call
// the harness makes, whatever serves it: ScadBuddy's in-process tools, remote
// plugin tools, and calls the permission seam refused or a human denied,
// which never reach a tool at all.
//
//   outcome   ok        the result came back without is_error
//             error     it came back with is_error (the tool failed, or was
//                       interrupted before it answered)
//             denied    a human denied its approval
//             refused   its approval expired, was cancelled or voided, or the
//                       turn stopped while it waited
//   timings   started_at is when the model's call was seen, finished_at when
//             its result was; for an outward call the span includes the wait
//             for a human.
//
// Inputs are stored as the approvals store them: the keyed HMAC and the
// scrubbed summary (log.ts). The result is not stored; its first characters
// are the `detail` of a failed call, redacted of the turn's secrets.

type Open = { name: string; tier: RiskTier; input: Record<string, unknown>; startedAt: Date }

export class TurnAuditor {
  private readonly open = new Map<string, Open>()
  private readonly verdicts = new Map<string, ApprovalVerdict>()
  private readonly audit: AuditLog
  private readonly context: {
    sessionId: string
    turnId: string
    actor: AuditActor
    tierOf: TierResolver
    secrets: () => readonly string[]
  }

  constructor(
    audit: AuditLog,
    context: { sessionId: string; turnId: string; actor: AuditActor; tierOf: TierResolver; secrets: () => readonly string[] },
  ) {
    this.audit = audit
    this.context = context
  }

  /**
   * The gate, with each verdict remembered for its call's row. A gate that
   * gives up (the turn stopped while the call waited) is a refusal; the
   * approval it had recorded is found by the call's id when `approvalFor` is
   * given.
   */
  gate(gate: ApprovalGate, approvalFor?: (toolUseId: string) => Promise<string | undefined>): ApprovalGate {
    return async (request) => {
      try {
        const verdict = await gate(request)
        this.verdicts.set(request.toolUseId, verdict)
        return verdict
      } catch (err) {
        const approvalId = await approvalFor?.(request.toolUseId).catch(() => undefined)
        this.verdicts.set(request.toolUseId, {
          approved: false,
          message: err instanceof Error ? err.message : String(err),
          ...(approvalId ? { approvalId } : {}),
        })
        throw err
      }
    }
  }

  /** Feed every event the turn maps, unscrubbed (the hash is of the full input). */
  async observe(e: ServerEvent): Promise<void> {
    if (e.type === 'tool.call') {
      this.open.set(e.id, {
        name: e.name,
        tier: this.context.tierOf(e.name) ?? 'outward',
        input: e.input,
        startedAt: new Date(),
      })
      return
    }
    if (e.type !== 'tool.result') return
    const call = this.open.get(e.id)
    if (!call) return
    this.open.delete(e.id)
    await this.write(e.id, call, e.ok, unwrapUntrusted(e.summary))
  }

  /** Calls with no result when the turn ended; `why` is the turn's reason. */
  async finish(why: string): Promise<void> {
    const left = [...this.open.entries()]
    this.open.clear()
    for (const [id, call] of left) await this.write(id, call, false, `no result: ${why}`)
  }

  private async write(toolUseId: string, call: Open, ok: boolean, summary: string): Promise<void> {
    const verdict = this.verdicts.get(toolUseId)
    this.verdicts.delete(toolUseId)
    let outcome: AuditOutcome
    if (verdict && !verdict.approved) outcome = verdict.decision === 'denied' ? 'denied' : 'refused'
    else outcome = ok ? 'ok' : 'error'
    const secrets = this.context.secrets()
    await this.audit.record({
      kind: 'tool_call',
      action: call.name,
      surface: 'harness',
      actor: this.context.actor,
      sessionId: this.context.sessionId,
      turnId: this.context.turnId,
      toolUseId,
      tier: call.tier,
      inputHash: this.audit.hash(call.name, call.input),
      inputSummary: this.audit.summarise(call.name, call.input, secrets),
      approvalId: verdict?.approvalId,
      outcome,
      ...(outcome === 'ok' ? {} : { detail: safeDetail(summary, secrets) }),
      startedAt: call.startedAt,
      finishedAt: new Date(),
    })
  }
}
