import { randomBytes } from 'node:crypto'
import type { SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import type { AuditEntry, AuditSink } from '../src/audit/log.js'
import { credentialDisplayLabel, credentialLabel } from '../src/credentials.js'
import { describeApiFailure } from '../src/harness/credentialErrors.js'
import { CredentialPool, NoUsableCredentialError } from '../src/harness/fallback.js'
import { kekFromBase64, type KekStatus } from '../src/secrets.js'
import { credentialFallbackEvent, tallyTurn } from '../src/sessions/turnCore.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// #3710: a credential's name is shown wherever a credential is named to the person, and
// nowhere else. Logs and audit rows keep `credentialLabel`, and the model is never told it.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const kekStatus: KekStatus = { ok: true, kek }
const KEY_A = 'sk-ant-api03-named-first-GwAA'
const KEY_B = 'sk-ant-api03-named-second-JwAA'

const row = { priority: 0, kind: 'claude_oauth_token' as const, base_url: null, last4: 'GwAA' }

describe('credentialDisplayLabel (#3710)', () => {
  it('is credentialLabel with the name quoted, and credentialLabel when there is none', () => {
    expect(credentialDisplayLabel({ ...row, name: 'Personal Max' })).toBe('credential 1 "Personal Max" (OAuth token …GwAA)')
    expect(credentialDisplayLabel({ ...row, name: null })).toBe(credentialLabel(row))
  })

  it('quotes a name as data: its quotes and backslashes are escaped', () => {
    expect(credentialDisplayLabel({ ...row, name: 'say "hi" \\ ok' })).toBe('credential 1 "say \\"hi\\" \\\\ ok" (OAuth token …GwAA)')
  })
})

describe('CredentialPool names for the person, logs without the name (#3710)', () => {
  async function pool() {
    const repo = new MemoryCredentials()
    const audited: AuditEntry[] = []
    const logged: string[] = []
    const audit = { record: (e: AuditEntry) => Promise.resolve(void audited.push(e)) } as unknown as AuditSink
    const a = await repo.create({ kind: 'anthropic_api_key', secret: KEY_A, name: 'Personal Max' }, kek)
    const b = await repo.create({ kind: 'anthropic_api_key', secret: KEY_B, name: 'Work' }, kek)
    return { repo, a, b, audited, logged, pool: new CredentialPool({ repo, kek: kekStatus, audit, log: (l) => logged.push(l) }) }
  }

  it('gives each candidate a display label with its name, and a label without', async () => {
    const { pool: p } = await pool()
    const [first] = await p.candidates()
    expect(first).toMatchObject({ label: 'credential 1 (API key …GwAA)', displayLabel: 'credential 1 "Personal Max" (API key …GwAA)' })
  })

  it('says which credentials are unusable by name in `shown`, never in `message`', async () => {
    const { repo, a, b, pool: p } = await pool()
    await repo.record(a.id, a.epoch, { kind: 'disabled', reason: 'HTTP 401' })
    await repo.record(b.id, b.epoch, { kind: 'cooling_down', until: new Date(Date.now() + 60_000), reason: 'HTTP 429' })
    const error = await p.candidates().catch((err: unknown) => err)
    expect(error).toBeInstanceOf(NoUsableCredentialError)
    const { message, shown } = error as NoUsableCredentialError
    expect(message).not.toContain('Personal Max')
    expect(message).toContain('credential 1 (API key …GwAA) is disabled')
    expect(shown).toContain('credential 1 "Personal Max" (API key …GwAA) is disabled')
    expect(shown).toContain('credential 2 "Work" (API key …JwAA) is rate limited until')
  })

  it('logs and audits a disable and a fallback without the name', async () => {
    const { pool: p, audited, logged } = await pool()
    const [first, second] = await p.candidates()
    await p.report(first!, { class: 'permanent', reason: 'HTTP 401' }, second)
    expect(audited.map((e) => e.action)).toEqual(['disable', 'fallback'])
    expect(JSON.stringify(audited)).not.toMatch(/Personal Max|Work/)
    expect(logged.join('\n')).not.toMatch(/Personal Max|Work/)
  })
})

describe('a turn names the credential to the person (#3710)', () => {
  const session = { id: 's1', ownCostUsd: 0, turns: 0 }
  const base = { stopped: undefined, interrupted: undefined, secrets: [], turnUsd: 0, cutUsd: 0 }

  it('adds the failure in its user-facing words beside the logged one, and keeps the outcome as logged', () => {
    const tally = tallyTurn(session, {
      ...base,
      result: undefined,
      refused: undefined,
      failure: 'the Claude credential (credential 1 (API key …GwAA)) was refused: HTTP 401',
      failureShown: 'the Claude credential (credential 1 "Personal Max" (API key …GwAA)) was refused: HTTP 401',
    })
    // `message` is what logs, flows and the outcome read; only the panel shows `shown`.
    expect(tally.tail.find((e) => e.type === 'error')).toEqual(
      expect.objectContaining({
        code: 'turn_failed',
        message: 'the Claude credential (credential 1 (API key …GwAA)) was refused: HTTP 401',
        shown: 'the Claude credential (credential 1 "Personal Max" (API key …GwAA)) was refused: HTTP 401',
      }),
    )
    expect(tally.outcome).toEqual({ kind: 'failed', message: 'the Claude credential (credential 1 (API key …GwAA)) was refused: HTTP 401' })
  })

  it('names the refused credential in the refusal it shows', () => {
    const result = { type: 'result', subtype: 'success', is_error: true, num_turns: 1, total_cost_usd: 0 } as unknown as SDKResultMessage
    const evidence = { status: 401, message: 'invalid x-api-key' }
    const tally = tallyTurn(session, {
      ...base,
      result,
      refused: { evidence, probe: 'refused', shownAs: 'credential 1 "Personal Max" (API key …GwAA)' },
      failure: undefined,
    })
    expect(tally.tail.find((e) => e.type === 'error')).toMatchObject({
      code: 'api_error',
      message: describeApiFailure(evidence, { probe: 'refused' }),
      shown: describeApiFailure(evidence, { probe: 'refused' }, 'credential 1 "Personal Max" (API key …GwAA)'),
    })
    expect(tally.outcome).toEqual({ kind: 'failed', message: describeApiFailure(evidence, { probe: 'refused' }) })
  })

  it('says which credential failed and which one the turn moved to', () => {
    const until = new Date('2026-11-08T14:00:00Z')
    expect(
      credentialFallbackEvent(
        's1',
        { displayLabel: 'credential 1 "Personal Max" (OAuth token …GwAA)', label: 'credential 1 (OAuth token …GwAA)' },
        { class: 'rate_limited', reason: 'HTTP 429', until },
        { label: 'credential 2 (API key …JwAA)' },
      ),
    ).toMatchObject({
      type: 'credential.fallback',
      sessionId: 's1',
      from: 'credential 1 "Personal Max" (OAuth token …GwAA)',
      to: 'credential 2 (API key …JwAA)',
      outcome: 'rate_limited',
      until: until.toISOString(),
    })
  })
})

describe('describeApiFailure names the credential when told which (#3710)', () => {
  it('puts the label after "the Claude credential", and leaves other wordings as they are', () => {
    const label = 'credential 1 "Work" (API key …JwAA)'
    expect(describeApiFailure({ status: 401, message: 'no' }, { probe: 'refused' }, label)).toBe(
      'the Claude credential (credential 1 "Work" (API key …JwAA)) was rejected (HTTP 401); check it under Settings → AI: no',
    )
    expect(describeApiFailure({ status: 429, message: 'slow' }, {}, label)).toBe(
      'the Claude credential (credential 1 "Work" (API key …JwAA)) is rate limited (HTTP 429); try again later: slow',
    )
    expect(describeApiFailure({ status: 500, message: 'x' }, {}, label)).toBe(describeApiFailure({ status: 500, message: 'x' }))
  })
})
