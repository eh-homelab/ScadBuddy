import { createHash, randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { acceptList, accepts, type PickerSession } from '../src/routes/workspacePicker.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { SessionResources } from '../src/sessions/touched.js'
import { grantAad, PluginOAuth } from '../src/plugins/oauthStore.js'
import { PluginStore } from '../src/plugins/registry.js'
import { toolSecrets } from '../src/plugins/secrets.js'
import { kekFromBase64, sealSecret } from '../src/secrets.js'
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
const kek = kekFromBase64(randomBytes(32).toString('base64'))

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
  let plugins: PluginStore
  let oauth: PluginOAuth
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    failures.length = 0
    touched = new SessionResources(db.sql, (err) => failures.push(err))
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    files = new SessionFiles(db.sql)
    oauth = new PluginOAuth(db.sql)
    plugins = new PluginStore(db.sql, { oauth })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, workspace: files })
    const saved = new MemoryCredentials()
    await saved.create({ kind: 'anthropic_api_key', secret: SECRET }, kek)
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
      // As main.ts wires it (plugins/secrets.ts `toolSecrets`, #5483): the saved credentials, then every plugin secret.
      workspaceSecrets: toolSecrets({ kek, credentials: saved, plugins, oauth }).fresh,
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

  it('finds an older session that matches the search, past the newest `limit` ones', async () => {
    const old = (await m.start(browser, { origin: 'chat', title: 'first' })).session
    await put(old.id, 'ancient.png', 'old', 'image/png')
    for (const n of [1, 2, 3]) {
      const s = (await m.start(browser, { origin: 'chat', title: `newer ${n}` })).session
      await put(s.id, `new-${n}.png`, `n${n}`, 'image/png')
    }
    expect((await pick('?accept=image/png&limit=2')).body.sessions.map((s) => s.id)).not.toContain(old.id)
    const found = await pick('?accept=image/png&limit=2&q=ANCIENT')
    expect(found.body.sessions.map((s) => [s.id, s.files.map((f) => f.path)])).toEqual([[old.id, ['ancient.png']]])
    // The search is literal text: `%` and `_` are no wildcards.
    expect((await pick('?accept=image/png&q=%25')).body.sessions).toEqual([])
    expect((await pick('?accept=image/png&q=new_')).body.sessions).toEqual([])
  })

  it('lists a sealed file it cannot open as unavailable, with the reason', async () => {
    const s = (await m.start(browser, { origin: 'chat', title: 'sealed' })).session
    await put(s.id, 'gone.png', 'x', 'image/png')
    await db.sql`
      UPDATE ai_session_files SET data = NULL, bytes_sha256 = NULL, blob_key = ${`workspace-${'a'.repeat(64)}`}, key_sealed = ${Buffer.from([0])}
      WHERE session_id = ${s.id}`
    // A file of another kind that can be read does not keep the session from being greyed.
    await put(s.id, 'notes.txt', 'readable')
    const { body } = await pick('?accept=image/png')
    const [listed] = body.sessions
    expect(listed!.unavailable).toMatch(/files of this kind cannot be read now: .*no blob store/)
    expect(listed!.files[0]!.unavailable).toMatch(/no blob store/)
    expect(listed!.other_files).toBe(1)
    // For a target that takes the readable file, the session is not greyed.
    expect((await pick('?accept=text/plain')).body.sessions[0]!.unavailable).toBeNull()
  })

  it('finds an older session that holds the only file the target takes, past the newest `limit` ones', async () => {
    const old = (await m.start(browser, { origin: 'chat', title: 'mesh work' })).session
    await put(old.id, 'out/bracket.STL', 'solid', 'model/stl')
    for (const n of [1, 2, 3]) {
      const s = (await m.start(browser, { origin: 'chat', title: `pictures ${n}` })).session
      await put(s.id, `bracket-${n}.png`, `n${n}`, 'image/png')
    }
    const found = await pick('?accept=.3mf,.stl&limit=2')
    expect(found.body.sessions.map((s) => [s.id, s.files.map((f) => f.path)])).toEqual([[old.id, ['out/bracket.STL']]])
    // A path match on a file the target does not take uses no place either.
    expect((await pick('?accept=.3mf,.stl&limit=1&q=bracket')).body.sessions.map((s) => s.id)).toEqual([old.id])
    // By media type and type wildcard too.
    expect((await pick('?accept=model/*&limit=1')).body.sessions.map((s) => s.id)).toEqual([old.id])
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

    // A remote plugin's header value, found without its scheme too.
    await plugins.create({ name: 'tracker', url: 'https://tracker.example/mcp', secret: 'Bearer plugin-header-secret-42' }, kek)
    await plugins.update('tracker', { enabled: true }, kek)
    await put(s.id, 'header.txt', 'token: plugin-header-secret-42')
    const header = await download('header.txt', `?expected_sha256=${sha('token: plugin-header-secret-42')}`)
    expect(header.status).toBe(422)
    expect(((await header.json()) as { detail: string }).detail).toMatch(/header value or OAuth token/)

    // An OAuth sign-in's tokens and client secret, on a plugin that is not even enabled.
    await plugins.create({ name: 'drive', url: 'https://drive.example/mcp' }, kek)
    const grant = {
      plugin: 'drive',
      plugin_url: 'https://drive.example/mcp',
      resource: 'https://drive.example/mcp',
      issuer: 'https://auth.example',
      token_endpoint: 'https://auth.example/token',
      client_id: 'client-1',
    }
    const sealed = sealSecret(
      kek,
      JSON.stringify({ access_token: 'oauth-access-token-7', refresh_token: 'oauth-refresh-token-8', client_secret: 'oauth-client-secret-9' }),
      grantAad(grant),
    )
    await db.sql`
      INSERT INTO ai_plugin_oauth
        (plugin, plugin_url, resource, issuer, token_endpoint, client_id, auth_method, registration, scope,
         secret_sealed, dek_sealed, kek_id, has_refresh, access_expires_at, status, last_error, grant_id,
         connected_by, connected_at, refreshed_at)
      VALUES (${grant.plugin}, ${grant.plugin_url}, ${grant.resource}, ${grant.issuer}, ${grant.token_endpoint},
              ${grant.client_id}, 'client_secret_post', 'configured', NULL, ${sealed.secretSealed}, ${sealed.dekSealed},
              ${sealed.kekId}, true, NULL, 'connected', NULL, ${'00000000-0000-4000-8000-000000000001'}, 'test', now(), NULL)`
    for (const value of ['oauth-access-token-7', 'oauth-refresh-token-8', 'oauth-client-secret-9']) {
      await put(s.id, `${value}.txt`, `x ${value} y`)
      expect((await download(`${value}.txt`, `?expected_sha256=${sha(`x ${value} y`)}`)).status, value).toBe(422)
    }
    await put(s.id, 'clean.txt', 'nothing secret here')
    expect((await download('clean.txt', `?expected_sha256=${sha('nothing secret here')}`)).status).toBe(200)
    // A plain download (the panel's) is as before.
    expect((await download('leak.txt', '')).status).toBe(200)
  })
})
