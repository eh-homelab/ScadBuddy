import { createHash } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { acceptList, accepts, type PickerSession } from '../src/routes/workspacePicker.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { SessionResources } from '../src/sessions/touched.js'
import { SessionFiles } from '../src/workspace/store.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #4703: the UI's workspace file picker. GET /api/v1/ai/workspace-files lists the
// sessions (and their files) the browser user may pick from, and a download with
// `expected_sha256` is the pick, refused once the file changed or when it holds a
// credential (routes/sessionFiles.ts).

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const sha = (bytes: Uint8Array | string) => createHash('sha256').update(bytes).digest('hex')
const SECRET = 'sk-ant-test-credential-0123456789'

describe('the accept list', () => {
  it('takes extensions, types and type wildcards, and refuses anything else', () => {
    expect(acceptList('.PNG, image/png,image/*')).toEqual(['.png', 'image/png', 'image/*'])
    expect(acceptList('')).toEqual([])
    expect(acceptList('png')).toBeUndefined()
    expect(acceptList('.png;x')).toBeUndefined()
    expect(accepts(['.stl', 'model/3mf'], 'parts/Box.STL', 'application/octet-stream')).toBe(true)
    expect(accepts(['.stl', 'model/3mf'], 'box.3mf', 'model/3mf')).toBe(true)
    expect(accepts(['image/*'], 'a.webp', 'image/webp')).toBe(true)
    expect(accepts(['image/*'], 'a.txt', 'text/plain')).toBe(false)
    expect(accepts(undefined, 'a.txt', 'text/plain')).toBe(true)
  })
})

describe.skipIf(skip !== undefined)(`the workspace file picker${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let files: SessionFiles
  let touched: SessionResources
  let app: ReturnType<typeof createApp>
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    touched = new SessionResources(db.sql, (err) => failures.push(err))
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    files = new SessionFiles(db.sql)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, workspace: files })
    app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
      workspace: files,
      workspaceSecrets: () => Promise.resolve([SECRET]),
    })
  })
  afterEach(async () => {
    m.abortAll()
    expect(failures).toEqual([])
    await drop()
  })

  const put = (sessionId: string, path: string, body: string, mediaType?: string) =>
    files.write({ kind: 'session', sessionId }, path, Buffer.from(body), mediaType ? { mediaType } : {})
  const touch = (sessionId: string, slug: string) =>
    touched.record({
      sessionId,
      tool: { name: 'update_source', risk: 'write' },
      input: { slug, base: '1'.repeat(40) },
      result: { content: [{ type: 'text', text: JSON.stringify({ slug, version: '2'.repeat(40) }) }] },
    })
  const pick = async (query: string, headers: Record<string, string> = UI_READ) => {
    const res = await app.request(`/api/v1/ai/workspace-files${query}`, { headers })
    return { status: res.status, body: (await res.json()) as { sessions: PickerSession[]; detail?: string } }
  }

  it("lists the sessions that touched a resource, newest first, with each one's accepted files", async () => {
    const older = (await m.start(browser, { origin: 'chat', title: 'older box work' })).session
    await touch(older.id, 'box')
    await put(older.id, 'box.png', 'png bytes', 'image/png')
    await put(older.id, 'notes.txt', 'text')
    const newer = (await m.start(agentA, { origin: 'mcp', title: 'newer box work' })).session
    await touch(newer.id, 'box')
    const other = (await m.start(browser, { origin: 'chat', title: 'a lid' })).session
    await touch(other.id, 'lid')
    await put(other.id, 'lid.png', 'lid', 'image/png')

    const { status, body } = await pick('?resource_type=model&resource_id=box&accept=.png,image/png')
    expect(status).toBe(200)
    expect(body.sessions.map((s) => s.id)).toEqual([newer.id, older.id])
    // A session with no file the target takes is still listed in the default list.
    expect(body.sessions[0]).toMatchObject({ title: 'newer box work', files: [], other_files: 0, unavailable: null, archived: false })
    expect(body.sessions[1]!.files).toEqual([
      expect.objectContaining({ path: 'box.png', media_type: 'image/png', size: 9, sha256: sha('png bytes'), unavailable: null }),
    ])
    expect(body.sessions[1]!.other_files).toBe(1)
  })

  it('lists every visible session that holds an accepted file in Advanced, archived ones too, and searches titles and paths', async () => {
    const a = (await m.start(browser, { origin: 'chat', title: 'Keychain ideas' })).session
    await put(a.id, 'renders/front.png', 'a', 'image/png')
    await put(a.id, 'renders/back.png', 'b', 'image/png')
    const archived = (await m.start(browser, { origin: 'chat', title: 'old' })).session
    await put(archived.id, 'Logo.png', 'c', 'image/png')
    await m.edits.update(archived.id, browser, { archived: true })
    const empty = (await m.start(browser, { origin: 'chat', title: 'nothing here' })).session
    const textOnly = (await m.start(browser, { origin: 'chat', title: 'text only' })).session
    await put(textOnly.id, 'readme.md', '# hi')

    const all = await pick('?accept=image/png')
    expect(all.body.sessions.map((s) => s.id).sort()).toEqual([a.id, archived.id].sort())
    expect(all.body.sessions.find((s) => s.id === archived.id)).toMatchObject({ archived: true })
    expect(all.body.sessions.map((s) => s.id)).not.toContain(empty.id)

    // A path match lists only the matching files; a title match every accepted file.
    const byPath = await pick('?accept=image/png&q=FRONT')
    expect(byPath.body.sessions.map((s) => [s.id, s.files.map((f) => f.path)])).toEqual([[a.id, ['renders/front.png']]])
    const byTitle = await pick('?accept=image/png&q=keychain')
    expect(byTitle.body.sessions.map((s) => [s.id, s.files.map((f) => f.path)])).toEqual([[a.id, ['renders/back.png', 'renders/front.png']]])
    expect((await pick('?accept=image/png&q=nothing')).body.sessions.map((s) => s.id)).toEqual([empty.id])
  })

  it('lists a sealed file it cannot open as unavailable, with the reason', async () => {
    const s = (await m.start(browser, { origin: 'chat', title: 'sealed' })).session
    await put(s.id, 'gone.png', 'x', 'image/png')
    // The backend's own table, which the row's trigger writes (as workspaceBlobs.pg.test.ts makes it).
    await db.sql`
      CREATE TABLE IF NOT EXISTS blob_refs (
        key text NOT NULL, holder_kind text NOT NULL, holder_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (key, holder_kind, holder_id))`
    await db.sql`
      UPDATE ai_session_files SET data = NULL, blob_key = ${`workspace-${'a'.repeat(64)}`}, key_sealed = ${Buffer.from([0])}
      WHERE session_id = ${s.id}`
    const { body } = await pick('?accept=image/png')
    const [listed] = body.sessions
    expect(listed!.unavailable).toMatch(/cannot be read now: .*no blob store/)
    expect(listed!.files[0]!.unavailable).toMatch(/no blob store/)
  })

  it('refuses a bad query', async () => {
    for (const query of [
      '?resource_type=model',
      '?resource_id=box',
      '?resource_type=nope&resource_id=box',
      '?accept=png',
      `?q=${'x'.repeat(201)}`,
      '?limit=0',
      '?limit=501',
    ]) {
      expect((await pick(query)).status, query).toBe(400)
    }
    expect((await pick('', { ...UI_READ, 'sec-fetch-site': 'cross-site' })).status).toBe(403)
  })

  it('refuses a pick whose file has changed, or that holds a credential', async () => {
    const s = (await m.start(browser, { origin: 'chat' })).session
    await put(s.id, 'thumb.png', 'first', 'image/png')
    const download = (path: string, query: string) =>
      app.request(`/api/v1/ai/sessions/${s.id}/files/${path}${query}`, { headers: UI_READ })

    const fresh = await download('thumb.png', `?expected_sha256=${sha('first')}`)
    expect(fresh.status).toBe(200)
    expect(await fresh.text()).toBe('first')

    await put(s.id, 'thumb.png', 'second', 'image/png')
    const stale = await download('thumb.png', `?expected_sha256=${sha('first')}`)
    expect(stale.status).toBe(409)
    expect(((await stale.json()) as { detail: string }).detail).toMatch(/changed since the list was read/)
    expect((await download('thumb.png', '?expected_sha256=nope')).status).toBe(400)

    await put(s.id, 'leak.txt', `key=${SECRET}`)
    expect((await download('leak.txt', `?expected_sha256=${sha(`key=${SECRET}`)}`)).status).toBe(422)
    // A plain download (the panel's) is as before.
    expect((await download('leak.txt', '')).status).toBe(200)
  })
})
