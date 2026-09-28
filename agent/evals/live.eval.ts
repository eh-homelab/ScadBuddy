import { mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import { bundledCliPath, sdkDeclaredCliVersion } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { resolveEvalCredential } from './credential.js'
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
}
const reports: ScenarioReport[] = []

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

  for (const scenario of SCENARIOS) {
    it(scenario.title, async () => {
      if (!found.ok) return
      const stateDir = await mkdtemp(path.join(os.tmpdir(), `scadbuddy-eval-${scenario.id}-`))
      await ensureStateDirs({ stateDir })
      const outcome = await runScenario(scenario, {
        paths: { stateDir },
        credential: found.credential,
        ...(found.model ? { model: found.model } : {}),
      })
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
      })
      console.log(`${formatReport(scenario.id, checks)}\n  model: ${outcome.model ?? 'unknown'}`)
      expect(checks.filter((c) => !c.pass)).toEqual([])
    }, 360_000)
  }
})
