import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { EventLog } from '../src/sessions/eventLog.js'
import { event, type Owner } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// EventLog.userTurnText (#2419): a turn's words found by its turnId, without reading the log.

const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }

describe.skipIf(!TEST_DATABASE_URL)(`EventLog.userTurnText${TEST_DATABASE_URL ? '' : ` (set ${TEST_DATABASE_URL_ENV})`}`, () => {
  let t: Throwaway
  let log: EventLog

  beforeEach(async () => {
    t = await throwawayDatabase()
    expect(await t.db.ready()).toBe(true)
    log = new EventLog(t.db.sql)
  })
  afterEach(async () => {
    await t?.drop()
  })

  async function session(): Promise<string> {
    const id = randomUUID()
    await t.db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
      VALUES (${id}, 'mcp', 'browser', 'browser', 'Someone', 'browser', 'browser', 'running', 10, 1, 'durable')`
    return id
  }

  it("returns that turn's words, from that session only", async () => {
    const [a, b] = [await session(), await session()]
    await log.append(a, [
      event({ type: 'user.turn', sessionId: a, turnId: 't1', text: 'make a box', author: BROWSER }),
      event({ type: 'assistant.text.delta', sessionId: a, messageId: 'm1', delta: 'ok' }),
      event({ type: 'user.turn', sessionId: a, turnId: 't2', text: 'paint it red', author: BROWSER }),
    ])
    await log.append(b, [event({ type: 'user.turn', sessionId: b, turnId: 't3', text: 'elsewhere', author: BROWSER })])

    expect(await log.userTurnText(a, 't1')).toBe('make a box')
    expect(await log.userTurnText(a, 't2')).toBe('paint it red')
    expect(await log.userTurnText(a, 't3')).toBeUndefined()
    expect(await log.userTurnText(a, 'missing')).toBeUndefined()
  })

  it('returns the last one logged when a turnId is logged twice (#3361)', async () => {
    const a = await session()
    await log.append(a, [event({ type: 'user.turn', sessionId: a, turnId: 't1', text: 'first', author: BROWSER })])
    await log.append(a, [event({ type: 'user.turn', sessionId: a, turnId: 't1', text: 'second', author: BROWSER })])
    expect(await log.userTurnText(a, 't1')).toBe('second')
  })
})
