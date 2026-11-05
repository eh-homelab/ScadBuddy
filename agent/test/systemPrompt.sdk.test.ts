import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { SYSTEM_PROMPT_DYNAMIC_BOUNDARY } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { UNTRUSTED_CONTENT_POLICY } from '../src/safety/untrusted.js'
import { systemAppend } from '../src/sessions/turnShared.js'
import type { SystemPromptBase } from '../src/sessions/turnSettings.js'
import { type FakeAnthropic, type RecordedRequest, startFakeAnthropic } from './support/fakeAnthropic.js'

// The system prompt's base (#5222) on the real SDK and its bundled Claude Code, against the
// local fake endpoint: what `system` each base sends, and that ScadBuddy's fixed part and the
// user's text are in it whichever it is.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const TOKEN = 'gw-system-prompt-token-0000111122223333'
const USER_TEXT = 'Answer in metric units only.'

type Block = { type?: string; text?: string; cache_control?: unknown }
const systemOf = (r: RecordedRequest) => (Array.isArray(r.body?.system) ? (r.body.system as Block[]) : [])
const systemText = (r: RecordedRequest) => systemOf(r).map((b) => b.text ?? '').join('\n')

describe.skipIf(cliMissing !== undefined)(`the system prompt's base on the real SDK${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let stateDir: string

  beforeEach(async () => {
    fake = await startFakeAnthropic(() => ({ text: 'ok' }))
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'system-prompt-'))
    await ensureStateDirs({ stateDir })
  })
  afterEach(async () => {
    await fake.close()
  })

  async function first(base: SystemPromptBase): Promise<RecordedRequest> {
    for await (const _ of runHarness({
      paths: { stateDir },
      credential: { kind: 'gateway', baseUrl: fake.url, secret: TOKEN },
      prompt: 'hi',
      model: 'claude-sonnet-4-5',
      systemPromptAppend: `${systemAppend()}\n\n${USER_TEXT}`,
      systemPromptBase: base,
    })) {
      // drained
    }
    return fake.messageCalls().at(-1)!
  }

  it('claude_code: the preset, then ScadBuddy text, the working directory moved out of it', async () => {
    const r = await first('claude_code')
    const text = systemText(r)
    expect(text).toContain("You are Claude Code, Anthropic's official CLI for Claude")
    expect(text).toContain(UNTRUSTED_CONTENT_POLICY)
    expect(text.indexOf(USER_TEXT)).toBeGreaterThan(text.indexOf(UNTRUSTED_CONTENT_POLICY))
    // excludeDynamicSections: the working directory is in the first user message, not the system prompt.
    expect(text).not.toContain(stateDir)
    expect(JSON.stringify(r.body?.messages?.[0])).toContain(stateDir)
    expect(JSON.stringify(r.body)).not.toContain(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
  })

  it('minimal: the SDK line and ScadBuddy text only, a tenth of the preset or less', async () => {
    const r = await first('minimal')
    const text = systemText(r)
    expect(text).toContain("You are a Claude agent, built on Anthropic's Claude Agent SDK.")
    expect(text).not.toContain('You are Claude Code')
    expect(text).toContain(UNTRUSTED_CONTENT_POLICY)
    expect(text).toContain(USER_TEXT)
    expect(JSON.stringify(r.body)).not.toContain(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
    // ScadBuddy's text is cached: a cache breakpoint on its block or after it.
    const at = systemOf(r).findIndex((b) => b.text?.includes(UNTRUSTED_CONTENT_POLICY))
    expect(systemOf(r).slice(at).some((b) => b.cache_control !== undefined)).toBe(true)
    const preset = await first('claude_code')
    expect(JSON.stringify(r.body?.system).length * 10).toBeLessThan(JSON.stringify(preset.body?.system).length)
  })
})
