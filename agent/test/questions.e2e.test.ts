import { fixedCredentials } from './support/fixedCredentials.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ASK_USER_QUESTION } from '../src/harness/questions.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { parseClientFrame } from '../src/sessions/clientProtocol.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { expectPanelAccepts, frontendClientMessages } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, collectUntil, manager, tempPaths } from './support/sessions.js'

// #940 end to end: a browser chat session on the real SDK and its bundled
// binary (against the local fake Anthropic endpoint) asks AskUserQuestion, the
// panel's own `question.answer` answers it, and the model reads the answer.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const TOKEN = 'gw-questions-e2e-token-4444555566667777'
const QUESTION = 'Approve this issue draft?'

describe.skipIf(skip !== undefined)(`questions against the real SDK${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let db: Database
  let drop: () => Promise<void>
  let stop: AbortController

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    stop = new AbortController()
  })
  afterEach(async () => {
    stop.abort()
    await fake.close()
    await drop()
  })

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  async function replica(audit?: AuditLog): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    return manager({
      sql: db.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      approvalPollMs: 50,
      ...(audit ? { audit } : {}),
    })
  }

  it('the panel answers a draft approval and the model reads the edited text', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: lastContent(r).includes('Shorter title please') ? 'Editing the draft.' : 'No answer came.' }
        : {
            toolUse: {
              name: ASK_USER_QUESTION,
              input: {
                questions: [
                  {
                    question: QUESTION,
                    header: 'Draft',
                    multiSelect: false,
                    options: [
                      { label: 'Approve', description: 'File it as written', preview: '## Title\n\nThe body.' },
                      { label: 'Cancel', description: 'Do not file it' },
                    ],
                  },
                ],
              },
            },
          }
    const audit = new AuditLog({ sql: db.sql, settings: () => ({ get: () => Promise.resolve(undefined), set: () => Promise.resolve() }), hashKey: Buffer.alloc(32, 3) })
    const m = await replica(audit)
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'draft an issue, let me approve it' })
    const events = await m.attach(session.id, browser, { signal: stop.signal })
    const seen = await collectUntil(events, (e) => e.event.type === 'question.asked')
    const asked = seen.at(-1)!.event
    if (asked.type !== 'question.asked') throw new Error('unreachable')
    expect(asked.questions[0]?.options[0]?.preview).toBe('## Title\n\nThe body.')
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'waiting_input', turnActive: true })

    // What the panel sends, through the socket's own parser.
    const { clientMessage } = await frontendClientMessages()
    const frame = JSON.stringify(
      clientMessage({ type: 'question.answer', sessionId: session.id, id: asked.id, answers: ['Shorter title please'] }),
    )
    const parsed = parseClientFrame(frame)
    if (!parsed.ok || parsed.value.type !== 'question.answer') throw new Error(`refused: ${JSON.stringify(parsed)}`)
    await m.questions.answer(browser, parsed.value)

    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('Shorter title please')
    const log = (await m.events.read(session.id, 0, 10_000)).map((e) => e.event)
    await expectPanelAccepts(log)
    const call = log.find((e) => e.type === 'tool.call')
    expect(call).toMatchObject({ id: asked.tool, name: ASK_USER_QUESTION, risk: 'read' })
    expect(log.map((e) => e.type)).toContain('question.resolved')
    // Audited at the tier the harness and the panel give it, not as an outward action.
    await expect
      .poll(async () => (await audit.list()).entries.filter((e) => e.action === ASK_USER_QUESTION).map((e) => e.tier))
      .toEqual(['read'])
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'idle', turnActive: false })
  }, 60_000)
})
