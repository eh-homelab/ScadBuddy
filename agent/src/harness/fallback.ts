import type { SDKMessage, SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { type AuditSink, SYSTEM_ACTOR } from '../audit/log.js'
import {
  type Credential,
  type CredentialEvent,
  type CredentialRepo,
  credentialLabel,
  describeUnusable,
  opensWith,
  soonestRecovery,
} from '../credentials.js'
import { type KekStatus, redact, SealError } from '../secrets.js'
import { classifyFailure, type FailureClass, type FailureEvidence, probeRateLimit } from './credentialErrors.js'
import { DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS, type HarnessRun, runHarness } from './run.js'

// Several Claude credentials, in priority order, with fallback (#1093).
//
// Each credential is a separate Claude Code process env (run.ts
// `credentialEnv`), so fallback is per query(): `runWithFallback` runs the
// turn on the first usable credential and, when it fails for a reason that
// is the credential's (credentialErrors.ts `classifyFailure`), runs it again
// on the next. Each credential is tried at most once per turn, which is the
// cap on how many keys spend on one failing turn; one whose failure is not
// the credential's (a bad request, a turn or budget limit) is not retried at
// all, since the next key would fail the same way.
//
// MID-TURN FAILURES RESUME, THEY DO NOT RESTART. Claude Code writes the
// session transcript as the turn goes: the user's prompt before the first
// model request, then every tool call and result. Measured on Claude Code
// 2.1.283 against the fake endpoint (test/fallback.e2e.test.ts): after a
// failed request, a query with `resume` and the prompt CONTINUE_PROMPT sends
// the model the original prompt, every finished tool call and its result,
// and then CONTINUE_PROMPT; the synthetic "API Error" message is left out.
// No tool runs twice. Sending the original prompt again instead would put it
// in the context twice. So the next credential always resumes the session
// Claude Code reported in its init message, when there was one; a query that
// never got that far runs again as it was.
//
// What the caller sees: the failed attempt's messages up to the failure
// (text and tool calls that did happen), never its synthetic error message
// or its error result; only the first init message; and one result, whose
// cost and turn count include the attempts before it.
//
// When to give up on an attempt:
//   - permanent (401, 403, billing) at Claude Code's first retry, before the
//     attempt made any progress: it retries a 401 with backoff, which only
//     delays the verdict. After progress the attempt is left to end on its
//     own, so its result reports what it spent.
//   - rate limited at the first retry, likewise, but only when there is a
//     credential to fall back to. With none, Claude Code's own waiting is
//     the best there is.
//   - transient (5xx, 529, network): Claude Code retries on the same
//     credential, at most `transientRetries` times when there is another
//     credential (CLAUDE_CODE_MAX_RETRIES), as many as it likes otherwise;
//     then the turn falls back for this call only. The credential is not
//     marked.

/** A credential ready for a query, with the row facts its outcome is recorded against. */
export type PooledCredential = {
  id: string
  /** The row's epoch when read; a failure recorded against an older one is ignored. */
  epoch: number
  /** For logs and audit: kind and last four characters, never the secret. */
  label: string
  credential: Credential
}

export type AttemptOutcome =
  | { class: 'ok' }
  | { class: 'permanent' | 'transient'; reason: string }
  | { class: 'rate_limited'; reason: string; until: Date }

/** Where a turn gets its credentials (CredentialPool; a fixed one in tests). */
export type CredentialSource = {
  /** Usable credentials, in priority order; throws (NoUsableCredentialError) when none is usable now. */
  candidates(): Promise<PooledCredential[]>
  /** `runWithFallback`'s `report` for one turn, which its audit rows name. */
  reporter(context: { sessionId?: string; turnId?: string }): FallbackOptions['report']
}

/** Thrown when no credential can be used now; `recoversAt` is the soonest one usable again, if any will be on its own. */
export class NoUsableCredentialError extends Error {
  override name = 'NoUsableCredentialError'
  readonly recoversAt: Date | undefined
  constructor(message: string, recoversAt: Date | undefined) {
    super(message)
    this.recoversAt = recoversAt
  }
}

/** Sent as the next credential's prompt when it resumes a turn another one started. */
export const CONTINUE_PROMPT =
  'The request to the model failed and was sent again with another Claude credential. Continue where you left off.'

/** Claude Code's retries of a transient failure on one credential, when there is another to fall back to. */
export const DEFAULT_TRANSIENT_RETRIES = 2

export type FallbackOptions = {
  /** Usable credentials, in priority order (`CredentialPool.candidates`). */
  candidates: readonly PooledCredential[]
  /** What each attempt learned about its credential; `next` is the one the turn falls back to. */
  report: (attempt: PooledCredential, outcome: AttemptOutcome, next: PooledCredential | undefined) => Promise<void>
  /** Runs one query; runHarness by default. */
  run?: (run: HarnessRun) => AsyncIterable<SDKMessage>
  /** When a rate-limited credential is usable again; asks the endpoint (credentialErrors.ts) by default. */
  rateLimitUntil?: (credential: Credential, model: string | undefined) => Promise<Date>
  transientRetries?: number
}

function linked(signal: AbortSignal | undefined): AbortController {
  const controller = new AbortController()
  if (signal?.aborted) controller.abort(signal.reason)
  else signal?.addEventListener('abort', () => controller.abort(signal.reason), { once: true })
  return controller
}

function textOf(message: Extract<SDKMessage, { type: 'assistant' }>): string {
  const content = message.message.content as unknown
  if (!Array.isArray(content)) return ''
  return content
    .map((b: unknown) => (typeof b === 'object' && b !== null && 'text' in b && typeof b.text === 'string' ? b.text : ''))
    .join(' ')
    .trim()
}

/** The failed model request a result reports, if it reports one. */
function apiFailure(
  result: SDKResultMessage | undefined,
  synthetic: FailureEvidence | undefined,
  lastRetry: FailureEvidence | undefined,
): FailureEvidence | undefined {
  if (!result) return synthetic
  if (result.subtype !== 'success' || !result.is_error) return undefined
  const status = result.api_error_status
  if (typeof status !== 'number' && result.terminal_reason !== 'api_error' && !synthetic) return undefined
  return {
    status: typeof status === 'number' ? status : (lastRetry?.status ?? null),
    category: synthetic?.category ?? lastRetry?.category,
    message: synthetic?.message || result.result,
  }
}

/** The final result, with what the earlier attempts spent added in. */
function withSpent(message: SDKMessage, usd: number, turns: number): SDKMessage {
  if (message.type !== 'result' || (usd === 0 && turns === 0)) return message
  return { ...message, total_cost_usd: message.total_cost_usd + usd, num_turns: message.num_turns + turns }
}

const isCredentialFailure = (c: FailureClass | undefined): c is 'permanent' | 'rate_limited' | 'transient' =>
  c === 'permanent' || c === 'rate_limited' || c === 'transient'

/**
 * Runs one turn on `candidates[0]`, falling back along the list as described
 * at the top of this file. Yields the SDK messages the caller should see; the
 * caller's `signal` stops every attempt.
 */
export async function* runWithFallback(
  base: Omit<HarnessRun, 'credential'>,
  options: FallbackOptions,
): AsyncGenerator<SDKMessage, void, undefined> {
  const runOne = options.run ?? runHarness
  const rateLimitUntil = options.rateLimitUntil ?? ((credential, model) => probeRateLimit(credential, { model }))
  const { candidates } = options
  if (candidates.length === 0) throw new NoUsableCredentialError('no Claude credential is usable', undefined)

  let prompt = base.prompt
  let session: Pick<HarnessRun, 'resume' | 'sessionId'> = {
    ...(base.resume === undefined ? {} : { resume: base.resume }),
    ...(base.sessionId === undefined ? {} : { sessionId: base.sessionId }),
  }
  let spentUsd = 0
  let spentTurns = 0
  let sawInit = false

  for (let i = 0; i < candidates.length; i++) {
    const current = candidates[i] as PooledCredential
    const next = candidates[i + 1]
    const controller = linked(base.signal)
    const { resume: _resume, sessionId: _sessionId, ...rest } = base
    const run: HarnessRun = {
      ...rest,
      ...session,
      prompt,
      credential: current.credential,
      signal: controller.signal,
      ...(i > 0
        ? {
            maxTurns: Math.max(1, (base.maxTurns ?? DEFAULT_MAX_TURNS) - spentTurns),
            maxBudgetUsd: Math.max((base.maxBudgetUsd ?? DEFAULT_MAX_BUDGET_USD) - spentUsd, 0.000001),
          }
        : {}),
      ...(next ? { maxRetries: options.transientRetries ?? DEFAULT_TRANSIENT_RETRIES } : {}),
    }

    /**
     * A synthetic error message, and an error result with anything after it:
     * shown only if the turn ends here. Any other result is passed on at once
     * (the session manager settles the turn on it).
     */
    const held: SDKMessage[] = []
    let result: SDKResultMessage | undefined
    let resultHeld = false
    let synthetic: FailureEvidence | undefined
    let lastRetry: FailureEvidence | undefined
    /** The retry this attempt was stopped at. */
    let refused: FailureEvidence | undefined
    let progressed = false
    let sessionSeen: string | undefined
    let model: string | undefined
    let thrown: unknown
    try {
      for await (const message of runOne(run)) {
        if (result) {
          if (resultHeld) held.push(message)
          else yield message
          continue
        }
        if (message.type === 'system' && message.subtype === 'init') {
          sessionSeen = message.session_id
          model = message.model
          if (sawInit) continue
          sawInit = true
          yield message
          continue
        }
        if (message.type === 'system' && message.subtype === 'api_retry') {
          const evidence: FailureEvidence = {
            status: message.error_status,
            category: message.error,
            message: `HTTP ${message.error_status ?? 'no response'}: ${message.error}`,
          }
          lastRetry = evidence
          const verdict = classifyFailure(evidence)
          if (!progressed && (verdict === 'permanent' || (verdict === 'rate_limited' && next))) {
            refused = evidence
            // Before `break`, whose return() would wait on the process. The
            // SDK closes Claude Code's input and sends SIGTERM 2 s later
            // (sdk.mjs 0.3.283), so a retry or two may still go out in that
            // grace; they are refused like the first and cost nothing.
            controller.abort(new Error(`the credential was refused (${evidence.message})`))
            break
          }
          yield message
          continue
        }
        if (message.type === 'assistant' && message.error) {
          synthetic ??= { category: message.error, message: textOf(message) }
          held.push(message)
          continue
        }
        if (message.type === 'result') {
          result = message
          resultHeld = apiFailure(message, synthetic, lastRetry) !== undefined
          if (resultHeld) {
            held.push(message)
            continue
          }
          for (const h of held) yield h
          held.length = 0
          yield withSpent(message, spentUsd, spentTurns)
          continue
        }
        // Anything else is the turn going on: whatever was held was not its end.
        for (const h of held) yield h
        held.length = 0
        synthetic = undefined
        if (message.type === 'assistant' || message.type === 'user' || message.type === 'stream_event') progressed = true
        yield message
      }
    } catch (err) {
      thrown = err
    } finally {
      // Stops the Claude Code process when the attempt was left early.
      controller.abort()
    }

    const release = function* (): Generator<SDKMessage> {
      for (const m of held) yield withSpent(m, spentUsd, spentTurns)
    }
    if (base.signal?.aborted) {
      yield* release()
      if (thrown !== undefined) throw thrown
      return
    }

    const failure = refused ?? apiFailure(result, synthetic, lastRetry)
    const verdict = failure ? classifyFailure(failure) : undefined
    if (!failure || !isCredentialFailure(verdict)) {
      // The endpoint answered: the credential works, whatever became of the turn.
      if (result) await options.report(current, { class: 'ok' }, undefined)
      yield* release()
      if (thrown !== undefined) throw thrown
      return
    }

    const reason = redact(failure.message || verdict, [current.credential.secret])
    const outcome: AttemptOutcome =
      verdict === 'rate_limited'
        ? { class: verdict, reason, until: await rateLimitUntil(current.credential, model) }
        : { class: verdict, reason }
    if (result) {
      spentUsd += result.total_cost_usd
      spentTurns += result.num_turns
    }
    await options.report(current, outcome, next)
    if (!next) {
      if (held.length === 0) throw new Error(`the Claude credential (${current.label}) was refused: ${reason}`)
      yield* release()
      if (thrown !== undefined) throw thrown
      return
    }
    if (sessionSeen !== undefined) {
      session = { resume: sessionSeen }
      prompt = CONTINUE_PROMPT
    }
  }
}

/**
 * The credentials a turn can use, and the record of what each attempt learned
 * (#1093). Audits every disable, cooldown, recovery and fallback as a
 * `credential` row (audit/log.ts), with no secret: credentials are named by
 * `credentialLabel`, and reasons are redacted before they get here.
 */
export class CredentialPool implements CredentialSource {
  private readonly repo: CredentialRepo
  private readonly kek: KekStatus
  private readonly audit: AuditSink | undefined
  private readonly log: (line: string) => void

  constructor(options: { repo: CredentialRepo; kek: KekStatus; audit?: AuditSink | undefined; log?: (line: string) => void }) {
    this.repo = options.repo
    this.kek = options.kek
    this.audit = options.audit
    this.log = options.log ?? ((line) => console.warn(line))
  }

  /** Usable credentials, decrypted, in priority order; throws NoUsableCredentialError when there is none. */
  async candidates(): Promise<PooledCredential[]> {
    const kek = this.kek
    if (!kek.ok) throw new NoUsableCredentialError(`no key-encryption key: ${kek.reason}`, undefined)
    const list = await this.repo.list()
    const out: PooledCredential[] = []
    for (const c of list) {
      if (c.status !== 'active' || !opensWith(c, kek)) continue
      let credential: Credential | undefined
      try {
        credential = await this.repo.reveal(kek.kek, c.id)
      } catch (err) {
        if (err instanceof SealError) continue
        throw err
      }
      if (credential) out.push({ id: c.id, epoch: c.epoch, label: credentialLabel(c), credential })
    }
    if (out.length === 0) throw new NoUsableCredentialError(describeUnusable(list, kek), soonestRecovery(list, kek))
    return out
  }

  /** `runWithFallback`'s `report`, with the turn it belongs to for the audit rows. */
  reporter(context: { sessionId?: string; turnId?: string } = {}): FallbackOptions['report'] {
    return (attempt, outcome, next) => this.report(attempt, outcome, next, context)
  }

  async report(
    attempt: PooledCredential,
    outcome: AttemptOutcome,
    next: PooledCredential | undefined,
    context: { sessionId?: string; turnId?: string } = {},
  ): Promise<void> {
    const event: CredentialEvent =
      outcome.class === 'ok'
        ? { kind: 'used' }
        : outcome.class === 'permanent'
          ? { kind: 'disabled', reason: outcome.reason }
          : outcome.class === 'rate_limited'
            ? { kind: 'cooling_down', until: outcome.until, reason: outcome.reason }
            : { kind: 'transient', reason: outcome.reason }
    let action: string | undefined
    let detail = ''
    try {
      const change = await this.repo.record(attempt.id, attempt.epoch, event)
      if (change?.recovered) {
        action = 'recover'
        detail = `${attempt.label} is usable again: its rate limit has passed`
      } else if (change && change.after === 'disabled' && change.before !== 'disabled') {
        action = 'disable'
        detail = `${attempt.label} is disabled until it is reset: ${'reason' in outcome ? outcome.reason : ''}`
      } else if (change && change.after === 'cooling_down' && outcome.class === 'rate_limited') {
        action = 'cooldown'
        detail = `${attempt.label} is rate limited until ${outcome.until.toISOString()}: ${outcome.reason}`
      }
    } catch (err) {
      this.log(`credentials: could not record ${outcome.class} for ${attempt.label}: ${(err as Error).message}`)
    }
    if (action) await this.record(action, detail, outcome.class === 'ok' ? 'ok' : 'error', context)
    if (next && outcome.class !== 'ok') {
      await this.record(
        'fallback',
        `${attempt.label} failed (${outcome.class}: ${outcome.reason}); this turn continues on ${next.label}`,
        'error',
        context,
      )
    }
  }

  private async record(
    action: string,
    detail: string,
    outcome: 'ok' | 'error',
    context: { sessionId?: string; turnId?: string },
  ): Promise<void> {
    this.log(`credentials: ${action}: ${detail}`)
    await this.audit?.record({
      kind: 'credential',
      action,
      surface: 'harness',
      actor: SYSTEM_ACTOR,
      outcome,
      sessionId: context.sessionId,
      turnId: context.turnId,
      detail,
    })
  }
}
