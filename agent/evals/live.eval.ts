import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import { bundledCliPath, sdkDeclaredCliVersion } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { scoreCitations } from './citations.js'
import { resolveEvalCredential } from './credential.js'
import { EVAL_BACKEND_URL_ENV, RealBackend } from './realBackend.js'
import { type CheckResult, formatReport, runScenario, score } from './runner.js'
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

const found = await resolveEvalCredential()
let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = !found.ok ? found.reason : cliMissing
if (skip) console.warn(`[evals] skipped: ${skip}`)

type ScenarioReport = {
  id: string
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

describe.skipIf(skip !== undefined)(`live evals${skip ? ` (skipped: ${skip})` : ''}`, () => {
  afterAll(async () => {
    const file = process.env.SCADBUDDY_EVAL_REPORT
    if (!file || !found.ok) return
    const report = {
      credential: found.source,
      sdkClaudeCodeVersion: await sdkDeclaredCliVersion(),
      passed: reports.filter((r) => r.pass).length,
      total: reports.length,
      scenarios: reports,
    }
    await writeFile(file, `${JSON.stringify(report, null, 2)}\n`)
  })

  const backendUrl = process.env[EVAL_BACKEND_URL_ENV]?.trim() || undefined
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
      const pass = checks.every((c) => c.pass)
      reports.push({
        id: scenario.id,
        title: scenario.title,
        pass,
        checks,
        toolCalls: outcome.toolCalls.map((c) => `${c.tool}${c.ok === false ? ' (error)' : ''}`),
        approvals: outcome.approvals.map((a) => a.toolName),
        ...(outcome.model ? { model: outcome.model } : {}),
        ...(outcome.claudeCodeVersion ? { claudeCodeVersion: outcome.claudeCodeVersion } : {}),
        ...(outcome.result ? { costUsd: outcome.result.total_cost_usd, turns: outcome.result.num_turns } : {}),
        durationMs: outcome.durationMs,
        finalText: outcome.finalText,
        backend: real ?? 'recorded',
        renders: outcome.renders.map((r) => ({
          jobId: r.jobId,
          status: r.status,
          ...(r.outputId ? { outputId: r.outputId } : {}),
          ...(r.model ? { parts: r.model.parts.map((p) => `${p.colour} z ${p.zMin}..${p.zMax}`) } : {}),
          ...(r.problem ? { problem: r.problem } : {}),
        })),
        ...citationReport(outcome.finalText),
      })
      console.log(`${formatReport(scenario.id, checks)}\n  model: ${outcome.model ?? 'unknown'}`)
      expect(checks.filter((c) => !c.pass)).toEqual([])
    }, TEST_TIMEOUT_MS)
  }
})
