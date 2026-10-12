import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { bundledCliPath, sdkDeclaredCliVersion } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { scoreCitations } from './citations.js'
import { resolveEvalCredential } from './credential.js'
import { EVAL_BACKEND_URL_ENV, RealBackend } from './realBackend.js'
import { type DurableEvalStack, durableEvalSkip, startDurableStack } from './durable.js'
import { type CheckResult, type EvalMode, evalModes, formatReport, type Outcome, runScenario, score } from './runner.js'
import { SCENARIOS } from './scenarios.js'

// Live evals (issue #259, spec §13 "Live evals"): the scenarios in
// ./scenarios.ts, answered by a real Claude model through the real harness.
// Never part of `pnpm test` or required CI: run with `pnpm evals` (see
// docs/ai/evals.md). Without a credential every scenario skips, with the
// reason in the suite's name and on stderr.
//
// SCADBUDDY_EVAL_REPORT=<file> writes a JSON report: per scenario, pass/fail
// per check, with the model and the SDK/Claude Code versions the run used.
//
// SCADBUDDY_EVAL_BACKEND_URL=<url> points the scenarios that seed nothing
// (`realBackend`) at a running ScadBuddy backend (the image), so the render the
// model asks for is OpenSCAD's and its checks read the real output (issue
// #1924, evals/render.ts). Without it those scenarios skip, with the reason in
// their name: a render checked on the recorded backend is not a real render.
//
// SCADBUDDY_EVAL_MODE=classic|durable|both (default both) picks the engine: the
// classic harness, or a durable session on a stack the run starts itself
// (evals/durable.ts: a Temporal dev server, agent-durable's worker, the agent's
// agent-tools worker, a throwaway Postgres database). The durable run skips, saying
// why, without uv, a Temporal CLI or SCADBUDDY_TEST_DATABASE_URL.

const found = await resolveEvalCredential()
let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = !found.ok ? found.reason : cliMissing
if (skip) console.warn(`[evals] skipped: ${skip}`)

const modes = evalModes(process.env.SCADBUDDY_EVAL_MODE)

type ScenarioReport = {
  id: string
  mode: EvalMode
  title: string
  pass: boolean
  checks: CheckResult[]
  toolCalls: string[]
  approvals: string[]
  model?: string
  claudeCodeVersion?: string
  costUsd?: number
  turns?: number
  durationMs: number
  finalText: string
  /** `recorded`, or the backend URL the scenario ran against. */
  backend: string
  renders: { jobId: string; status: string; outputId?: string; parts?: string[]; problem?: string }[]
  /** Per suggestion of the reply (evals/citations.ts), when it made any. */
  citations?: { suggestion: string; cited: boolean; unresolved: string[]; unsupported: string[]; unchecked: string[] }[]
}
const reports: ScenarioReport[] = []

// One scenario's whole budget, and the vitest timeout derived from it (#2110).
// The run may take its 300 s abort and a real render's collection its 180 s,
// so the test has room for both; `budgetMs` still cuts the collection to what
// is left, so a scenario that runs long is reported (with its unsettled render)
// rather than killed by vitest before `reports.push`.
const RUN_TIMEOUT_MS = 300_000
const REPORT_MARGIN_MS = 20_000
const SCENARIO_BUDGET_MS = RUN_TIMEOUT_MS + 180_000
const TEST_TIMEOUT_MS = SCENARIO_BUDGET_MS + REPORT_MARGIN_MS

function citationReport(reply: string): Pick<ScenarioReport, 'citations'> {
  const scores = scoreCitations(reply)
  if (!scores.length) return {}
  return {
    citations: scores.map((s) => ({
      suggestion: s.text,
      cited: s.cited,
      unresolved: s.unresolved,
      unsupported: s.unsupported,
      unchecked: s.unchecked,
    })),
  }
}

function report(mode: EvalMode, scenario: (typeof SCENARIOS)[number], outcome: Outcome, checks: CheckResult[], backend: string): void {
  reports.push({
    id: scenario.id,
    mode,
    title: scenario.title,
    pass: checks.every((c) => c.pass),
    checks,
    toolCalls: outcome.toolCalls.map((c) => `${c.tool}${c.ok === false ? ' (error)' : ''}`),
    approvals: outcome.approvals.map((a) => a.toolName),
    ...(outcome.model ? { model: outcome.model } : {}),
    ...(outcome.claudeCodeVersion ? { claudeCodeVersion: outcome.claudeCodeVersion } : {}),
    ...(outcome.result?.total_cost_usd !== undefined ? { costUsd: outcome.result.total_cost_usd } : {}),
    ...(outcome.result?.num_turns !== undefined ? { turns: outcome.result.num_turns } : {}),
    durationMs: outcome.durationMs,
    finalText: outcome.finalText,
    backend,
    renders: outcome.renders.map((r) => ({
      jobId: r.jobId,
      status: r.status,
      ...(r.outputId ? { outputId: r.outputId } : {}),
      ...(r.model ? { parts: r.model.parts.map((p) => `${p.colour} z ${p.zMin}..${p.zMax}`) } : {}),
      ...(r.problem ? { problem: r.problem } : {}),
    })),
    ...citationReport(outcome.finalText),
  })
  console.log(`${formatReport(`${scenario.id} [${mode}]`, checks)}\n  model: ${outcome.model ?? 'unknown'}`)
}

const backendUrl = process.env[EVAL_BACKEND_URL_ENV]?.trim() || undefined

describe.skipIf(skip !== undefined)(`live evals${skip ? ` (skipped: ${skip})` : ''}`, () => {
  afterAll(async () => {
    const file = process.env.SCADBUDDY_EVAL_REPORT
    if (!file || !found.ok) return
    const out = {
      credential: found.source,
      modes,
      sdkClaudeCodeVersion: await sdkDeclaredCliVersion(),
      passed: reports.filter((r) => r.pass).length,
      total: reports.length,
      scenarios: reports,
    }
    await writeFile(file, `${JSON.stringify(out, null, 2)}\n`)
  })

  describe.skipIf(!modes.includes('classic'))('classic', () => {
    for (const scenario of SCENARIOS) {
      const noBackend = scenario.realBackend && !backendUrl
      const title = noBackend ? `${scenario.title} (skipped: ${EVAL_BACKEND_URL_ENV} is not set)` : scenario.title
      it.skipIf(noBackend)(title, async () => {
        if (!found.ok) return
        const real = scenario.realBackend && backendUrl ? backendUrl : undefined
        const stateDir = await mkdtemp(path.join(os.tmpdir(), `scadbuddy-eval-${scenario.id}-`))
        let outcome: Awaited<ReturnType<typeof runScenario>>
        try {
          await ensureStateDirs({ stateDir })
          outcome = await runScenario(
            scenario,
            {
              paths: { stateDir },
              credential: found.credential,
              ...(found.model ? { model: found.model } : {}),
              timeoutMs: RUN_TIMEOUT_MS,
              budgetMs: SCENARIO_BUDGET_MS,
              // A real render takes seconds, not the recorded backend's none.
              ...(real ? { renderWaitMs: 120_000, collect: { pollMs: 1000, deadlineMs: SCENARIO_BUDGET_MS - RUN_TIMEOUT_MS } } : {}),
            },
            ...(real ? [new RealBackend(real)] : []),
          )
        } finally {
          await rm(stateDir, { recursive: true, force: true })
        }
        const checks = score(scenario, outcome)
        report('classic', scenario, outcome, checks, real ?? 'recorded')
        expect(checks.filter((c) => !c.pass)).toEqual([])
      }, TEST_TIMEOUT_MS)
    }
  })

  const durableSkip = durableEvalSkip()
  if (modes.includes('durable') && durableSkip) console.warn(`[evals] durable skipped: ${durableSkip}`)
  describe.skipIf(!modes.includes('durable') || durableSkip !== undefined)(`durable${durableSkip ? ` (skipped: ${durableSkip})` : ''}`, () => {
    // One stack for every scenario, each in its own durable session; the backend URL
    // a real-backend scenario uses is chosen per run, so render waits are the longer one.
    let stack: DurableEvalStack | undefined
    beforeAll(async () => {
      if (!found.ok) return
      stack = await startDurableStack({
        credential: found.credential,
        ...(found.model ? { model: found.model } : {}),
        ...(backendUrl ? { renderWaitMs: 120_000 } : {}),
      })
    }, 300_000)
    afterAll(async () => {
      await stack?.close()
    }, 60_000)

    for (const scenario of SCENARIOS) {
      const noBackend = scenario.realBackend && !backendUrl
      const title = noBackend ? `${scenario.title} (skipped: ${EVAL_BACKEND_URL_ENV} is not set)` : scenario.title
      it.skipIf(noBackend)(title, async () => {
        if (!stack) return
        const real = scenario.realBackend && backendUrl ? backendUrl : undefined
        const outcome = await stack.run(
          scenario,
          { timeoutMs: RUN_TIMEOUT_MS, ...(real ? { collect: { pollMs: 1000, deadlineMs: SCENARIO_BUDGET_MS - RUN_TIMEOUT_MS } } : {}) },
          ...(real ? [new RealBackend(real)] : []),
        )
        const checks = score(scenario, outcome)
        report('durable', scenario, outcome, checks, real ?? 'recorded')
        expect(checks.filter((c) => !c.pass)).toEqual([])
      }, TEST_TIMEOUT_MS)
    }
  })
})
