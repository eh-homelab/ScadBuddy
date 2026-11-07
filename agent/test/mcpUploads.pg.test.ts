import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { SessionFiles, type WorkspaceRef } from '../src/workspace/store.js'
import { appFetch, connect, firstText, services, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #4550: PUT /mcp/workspace/<path> writes into ai_session_files under the caller's
// principal workspace, and a later /mcp tool call by the same token finds the file
// at the answered name and sha256. Another token's workspace does not see it.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(24, 7)])

describe.skipIf(skip !== undefined)(`/mcp uploads in Postgres${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let files: SessionFiles

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    files = new SessionFiles(db.sql, { limits: { fileMaxBytes: 1000, workspaceMaxBytes: 2500 } })
  })
  afterEach(async () => {
    await drop()
  })

  it("stores the file in the caller's principal workspace, where its tools find it", async () => {
    const { app, tokens } = testApp({ services: services({ workspace: files }) })
    const { token, record } = await tokens.mint({ name: 'plugin', tier: 'write' })
    const other = await tokens.mint({ name: 'other', tier: 'write' })
    const res = await appFetch(app, { headers: { authorization: `Bearer ${token}` } })('http://scadbuddy.test/mcp/workspace/thumbs/a.png', {
      method: 'PUT',
      body: PNG,
    })
    expect(res.status).toBe(201)
    const answer = (await res.json()) as { name: string; mediaType: string; size: number; sha256: string }
    expect(answer).toEqual({ name: 'thumbs/a.png', mediaType: 'image/png', size: PNG.length, sha256: createHash('sha256').update(PNG).digest('hex') })

    const owner: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: `token:${record.id}` }
    const stored = await files.read(owner, 'thumbs/a.png')
    expect(stored?.bytes.equals(PNG)).toBe(true)
    expect(stored?.source).toEqual({ kind: 'upload', by: 'mcp' })

    const client = await connect(app, { headers: { authorization: `Bearer ${token}` } })
    try {
      const listed = await client.callTool({ name: 'workspace_list', arguments: {} })
      expect(JSON.stringify(firstText(listed))).toContain(answer.sha256)
    } finally {
      await client.close()
    }
    const theirs: WorkspaceRef = { kind: 'principal', principalKind: 'bearer', principalId: `token:${other.record.id}` }
    expect(await files.list(theirs)).toEqual([])
  })

  it('refuses an upload that would pass the workspace quota, and writes nothing', async () => {
    const { app, tokens } = testApp({ services: services({ workspace: files }) })
    const { token } = await tokens.mint({ name: 'plugin', tier: 'write' })
    const put = (path: string) =>
      appFetch(app, { headers: { authorization: `Bearer ${token}` } })(`http://scadbuddy.test/mcp/workspace/${path}`, {
        method: 'PUT',
        body: Buffer.alloc(900, 0x20),
      })
    expect((await put('a.stl')).status).toBe(201)
    expect((await put('b.stl')).status).toBe(201)
    const third = await put('c.stl')
    expect(third.status).toBe(413)
    expect(((await third.json()) as { error: string }).error).toContain('at most 2500')
  })
})
