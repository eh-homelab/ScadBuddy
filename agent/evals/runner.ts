import type { SDKMessage, SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import type { Credential } from '../src/credentials.js'
import type { HarnessPaths } from '../src/harness/options.js'
import type { ApprovalGate, ApprovalRequest, RiskTier, ToolDecision } from '../src/harness/permissions.js'
import { runHarness } from '../src/harness/run.js'
import { ALL_TOOLS, tierOf } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { createHarnessServer, SERVER_NAME } from '../src/tools/projections.js'
import { EVAL_BACKEND_URL, EvalBackend } from './backend.js'

// Runs one eval scenario through the REAL harness (src/harness/run.ts
// `runHarness`: the Agent SDK and its bundled Claude Code binary) with the
// real ScadBuddy tool registry (src/tools/, projected in-process by
// `createHarnessServer`, spec §5.1) against the recorded backend in
// ./backend.ts. Which model answers is the caller's choice: the local fake
// Anthropic endpoint for the deterministic run in CI (test/evals.test.ts), or
// the configured credential for a live run (./live.eval.ts).
//
// Outward calls go to an approval gate that records the request and DENIES it:
// an eval never approves anything, so "the approval gate was hit" and "nothing
// outward reached the backend" can both be checked (spec §8.2).

export const EVAL_TOOL_PREFIX = `mcp__${SERVER_NAME}__`

export const EVAL_DENIAL =
  'Not approved: the person reviewing this request declined it, so nothing was sent. Tell the user it was not done.'


export type ToolCall = {
  id: string
  /** As the SDK reports it: `mcp__scadbuddy__render_model`. */
  name: string
  /** The registry name: `render_model`. */
  tool: string
  input: Record<string, unknown>
  tier: RiskTier
  /** Undefined when no result came back (the run stopped first). */
  ok?: boolean
  result?: string
}

export type Outcome = {
  scenario: string
  toolCalls: ToolCall[]
  /** Every request the approval gate saw. */
  approvals: Pick<ApprovalRequest, 'toolName' | 'input' | 'toolUseId' | 'tier'>[]
  decisions: { toolName: string; decision: ToolDecision['decision']; tier: RiskTier }[]
  backend: EvalBackend
  result?: SDKResultMessage
  /** The last assistant text of the run. */
  finalText: string
  /** The model the run used, from the SDK's init message. */
  model?: string
  claudeCodeVersion?: string
  /** A thrown error that ended the run without a result message. */
  error?: string
  durationMs: number
}

export type CheckResult = { name: string; pass: boolean; detail?: string }

/** A deterministic check: `true` passes, a string is the reason it failed. */
export type Check = { name: string; run: (o: Outcome) => true | string }

export type Scenario = {
  id: string
  title: string
  /** Seeds the backend and returns the prompt and the scripted model turns for the deterministic run. */
  prepare(backend: EvalBackend): { prompt: string; script: ScriptedTurn[] }
  checks: Check[]
}

/** What the scripted model says on one call: text, or one tool call (registry name, without the prefix). */
export type ScriptedTurn = { text: string } | { tool: string; input: Record<string, unknown> }

export type RunOptions = {
  paths: HarnessPaths
  credential: Credential
  /** Model id or alias; the SDK's default when omitted. */
  model?: string
  maxTurns?: number
  maxBudgetUsd?: number
  /** Whole-run deadline, so a stuck live run cannot hang the job. */
  timeoutMs?: number
  stderr?: (line: string) => void
}

function blocks(content: unknown): Record<string, unknown>[] {
  return Array.isArray(content) ? (content as Record<string, unknown>[]) : []
}

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  return blocks(content)
    .map((b) => (b.type === 'text' && typeof b.text === 'string' ? b.text : ''))
    .join('')
}

/** Folds the SDK message stream into tool calls with their results, and the last assistant text. */
export function foldMessages(messages: SDKMessage[]): Pick<Outcome, 'toolCalls' | 'finalText' | 'model' | 'claudeCodeVersion' | 'result'> {
  const toolCalls: ToolCall[] = []
  const byId = new Map<string, ToolCall>()
  let finalText = ''
  let model: string | undefined
  let claudeCodeVersion: string | undefined
  let result: SDKResultMessage | undefined
  for (const m of messages) {
    if (m.type === 'system' && m.subtype === 'init') {
      model = m.model
      claudeCodeVersion = m.claude_code_version
    } else if (m.type === 'assistant' && m.parent_tool_use_id === null) {
      for (const b of blocks(m.message.content)) {
        if (b.type === 'text' && typeof b.text === 'string' && b.text.trim()) finalText = b.text
        if (b.type === 'tool_use' && typeof b.id === 'string' && typeof b.name === 'string' && !byId.has(b.id)) {
          const input = typeof b.input === 'object' && b.input !== null ? (b.input as Record<string, unknown>) : {}
          const call: ToolCall = {
            id: b.id,
            name: b.name,
            tool: b.name.startsWith(EVAL_TOOL_PREFIX) ? b.name.slice(EVAL_TOOL_PREFIX.length) : b.name,
            input,
            tier: tierOf(b.name) ?? 'outward',
          }
          byId.set(b.id, call)
          toolCalls.push(call)
        }
      }
    } else if (m.type === 'user' && m.parent_tool_use_id === null) {
      for (const b of blocks(m.message.content)) {
        const call = b.type === 'tool_result' && typeof b.tool_use_id === 'string' ? byId.get(b.tool_use_id) : undefined
        if (!call) continue
        call.ok = b.is_error !== true
        call.result = textOf(b.content)
      }
    } else if (m.type === 'result') {
      result = m
    }
  }
  return { toolCalls, finalText, model, claudeCodeVersion, result }
}

/** Runs a scenario's prompt through the harness and returns what happened. */
export async function runScenario(
  scenario: Scenario,
  options: RunOptions,
  backend = new EvalBackend(),
): Promise<Outcome & { script: ScriptedTurn[] }> {
  const { prompt, script } = scenario.prepare(backend)
  const approvals: Outcome['approvals'] = []
  const decisions: Outcome['decisions'] = []
  const gate: ApprovalGate = (request) => {
    approvals.push({ toolName: request.toolName, input: request.input, toolUseId: request.toolUseId, tier: request.tier })
    return Promise.resolve({ approved: false, message: EVAL_DENIAL })
  }
  const services = {
    backend: createBackendClient(EVAL_BACKEND_URL, backend.fetch),
    pending: new PendingActionStore(),
    pollIntervalMs: 5,
    renderWaitMs: 5_000,
    operationFollowMs: 15 * 60_000,
  }
  const principal = { id: `eval:${scenario.id}`, kind: 'browser' as const, tiers: tiersUpTo('outward') }
  const stop = new AbortController()
  const timer = setTimeout(() => stop.abort(new Error('eval timed out')), options.timeoutMs ?? 300_000)
  const started = Date.now()
  const messages: SDKMessage[] = []
  let error: string | undefined
  try {
    const query = runHarness({
      paths: options.paths,
      credential: options.credential,
      prompt,
      ...(options.model !== undefined ? { model: options.model } : {}),
      maxTurns: options.maxTurns ?? 12,
      maxBudgetUsd: options.maxBudgetUsd ?? 0.5,
      signal: stop.signal,
      mcpServers: { [SERVER_NAME]: createHarnessServer(ALL_TOOLS, services, principal) },
      tierOf,
      approvalGate: gate,
      onDecision: (toolName, decision) => decisions.push({ toolName, decision: decision.decision, tier: decision.tier }),
      // No systemPromptAppend: a session (src/sessions/manager.ts) sends
      // none, and a defence the eval adds on its own would make the injection
      // scenario pass for a prompt that never ships.
      ...(options.stderr ? { stderr: options.stderr } : {}),
    })
    for await (const m of query) messages.push(m)
  } catch (err) {
    // For an error result (max turns, budget) the SDK yields the result and
    // then throws (test/run.test.ts); the result is what counts then.
    if (!messages.some((m) => m.type === 'result')) error = err instanceof Error ? err.message : String(err)
  } finally {
    clearTimeout(timer)
  }
  return {
    scenario: scenario.id,
    ...foldMessages(messages),
    approvals,
    decisions,
    backend,
    ...(error !== undefined ? { error } : {}),
    durationMs: Date.now() - started,
    script,
  }
}

export function score(scenario: Scenario, outcome: Outcome): CheckResult[] {
  return scenario.checks.map((check) => {
    let verdict: true | string
    try {
      verdict = check.run(outcome)
    } catch (err) {
      verdict = `check threw: ${err instanceof Error ? err.message : String(err)}`
    }
    return verdict === true ? { name: check.name, pass: true } : { name: check.name, pass: false, detail: verdict }
  })
}

/** One line per check, for logs and the job summary. */
export function formatReport(id: string, results: CheckResult[]): string {
  const passed = results.every((r) => r.pass)
  return [
    `${passed ? 'PASS' : 'FAIL'} ${id}`,
    ...results.map((r) => `  ${r.pass ? 'ok  ' : 'FAIL'} ${r.name}${r.detail ? `: ${r.detail}` : ''}`),
  ].join('\n')
}
