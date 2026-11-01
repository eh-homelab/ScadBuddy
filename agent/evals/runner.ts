import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { ownPluginTierOf } from '../src/harness/ownPlugin.js'
import type { ApprovalRequest, RiskTier, ToolDecision } from '../src/harness/permissions.js'
import { tierOf } from '../src/tools/index.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { type CollectOptions, DEFAULT_COLLECT_DEADLINE_MS, type RenderedJob, type ScenarioBackend } from './render.js'

// What an eval scenario is, what a run of one records, and how it is scored. The runs
// go through the one runtime (./durable.ts: an AgentSession, as every session's turn
// runs, plan 2026-10-13 Step 6.6), answered by the local fake Anthropic endpoint in CI
// (test/evals.durable.e2e.test.ts) or the configured credential live (./live.eval.ts).
//
// Outward calls park on the session's gate, and the run DENIES each one: an eval never
// approves anything, so "the approval gate was hit" and "nothing outward reached the
// backend" can both be checked (spec §8.2).

export const EVAL_TOOL_PREFIX = `mcp__${SERVER_NAME}__`

/** The registry name of a tool as the model sees it: `mcp__scadbuddy__x` is `x`. */
export function registryName(name: string): string {
  return name.startsWith(EVAL_TOOL_PREFIX) ? name.slice(EVAL_TOOL_PREFIX.length) : name
}

export type ToolCall = {
  id: string
  /** As the model saw it: `mcp__scadbuddy__render_model`. */
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
  /** The recorded backend (evals/backend.ts), or the image behind a recorder (evals/realBackend.ts). */
  backend: ScenarioBackend
  /** Every render the run started, followed to its end and its saved output's 3MF (evals/render.ts). */
  renders: RenderedJob[]
  result?: RunResult
  /** The last assistant text of the run. */
  finalText: string
  /** The model the run used, from the SDK's init message. */
  model?: string
  claudeCodeVersion?: string
  /** A thrown error that ended the run without a result message. */
  error?: string
  durationMs: number
}

/** How the run ended: the SDK's result message. An SDKResultMessage is one. */
export type RunResult = { subtype: string; total_cost_usd?: number; num_turns?: number }

export type CheckResult = { name: string; pass: boolean; detail?: string }

/** A deterministic check: `true` passes, a string is the reason it failed. */
export type Check = { name: string; run: (o: Outcome) => true | string }

export type Scenario = {
  id: string
  title: string
  /** Seeds the backend and returns the prompt and the scripted model turns for the deterministic run. */
  prepare(backend: ScenarioBackend): { prompt: string; script: ScriptedTurn[] }
  checks: Check[]
  /**
   * Seeds nothing, so a live run may point it at a real backend
   * (SCADBUDDY_EVAL_BACKEND_URL, evals/live.eval.ts); the others need the
   * recorded backend's seeded models and outputs.
   */
  realBackend?: boolean
}

/** What the scripted model says on one call: text, or one tool call (registry name, without the prefix). */
export type ScriptedTurn = { text: string } | { tool: string; input: Record<string, unknown> }

/** How the scorer follows a run's renders, and the whole scenario's budget. */
export type CollectBudget = {
  /** How the scorer follows the run's renders afterwards (evals/render.ts). */
  collect?: CollectOptions
  /**
   * The whole scenario's budget, run and render collection together, from when the run
   * starts. The collection's deadline is cut to what is left of it, so a slow run cannot
   * push the scenario past the test's own timeout and drop it from the report (#2110).
   */
  budgetMs?: number
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
  let result: RunResult | undefined
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
            tool: registryName(b.name),
            input,
            // As the run tiers them (harness/run.ts `harnessTierOf`): the own plugin's Skill and Agent first.
            tier: ownPluginTierOf(b.name) ?? tierOf(b.name) ?? 'outward',
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

/** The collection's options, its deadline cut to what is left of `budgetMs` after `elapsedMs` (#2110). */
export function collectWithin(options: CollectBudget, elapsedMs: number): CollectOptions {
  const collect = options.collect ?? {}
  if (options.budgetMs === undefined) return collect
  const left = Math.max(0, options.budgetMs - elapsedMs)
  return { ...collect, deadlineMs: Math.min(collect.deadlineMs ?? DEFAULT_COLLECT_DEADLINE_MS, left) }
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
