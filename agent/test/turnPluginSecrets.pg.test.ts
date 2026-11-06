import { randomBytes } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { grantAad, PluginOAuth } from '../src/plugins/oauthStore.js'
import { PluginStore } from '../src/plugins/registry.js'
import { readPluginSecrets } from '../src/plugins/secrets.js'
import { kekFromBase64, sealSecret } from '../src/secrets.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { harnessTools } from '../src/tools/harness.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { SessionFiles } from '../src/workspace/store.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #5480: a turn without Temporal (the in-process harness, tools/harness.ts) checks an
// upload against the same list as a ToolCall and the UI's pick: the saved credentials and
// every plugin secret (plugins/secrets.ts readPluginSecrets, as main.ts builds
// `toolServices.secrets`), with the turn's own loaded headers on top.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const kek = kekFromBase64(randomBytes(32).toString('base64'))
const SAVED = 'sk-ant-saved-credential-0123456789'
const REFRESH = 'oauth-refresh-token-8'
const CLIENT_SECRET = 'oauth-client-secret-9'
const UNSAVED_HEADER = 'Bearer header-signed-in-this-turn-42'

const backend = setupServer()
beforeAll(() => backend.listen({ onUnhandledFrame: 'error' }))
afterEach(() => backend.resetHandlers())
afterAll(() => backend.close())

describe.skipIf(skip !== undefined)(`a non-durable turn's uploads${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let files: SessionFiles
  let oauth: PluginOAuth
  let plugins: PluginStore

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    files = new SessionFiles(db.sql)
    oauth = new PluginOAuth(db.sql)
    plugins = new PluginStore(db.sql, { oauth })
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner, workspace: files })
    await plugins.create({ name: 'drive', url: 'https://drive.example/mcp' }, kek)
    const grant = {
      plugin: 'drive',
      plugin_url: 'https://drive.example/mcp',
      resource: 'https://drive.example/mcp',
      issuer: 'https://auth.example',
      token_endpoint: 'https://auth.example/token',
      client_id: 'client-1',
    }
    const sealed = sealSecret(kek, JSON.stringify({ access_token: 'oauth-access-token-7', refresh_token: REFRESH, client_secret: CLIENT_SECRET }), grantAad(grant))
    await db.sql`
      INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint, client_id, auth_method, registration, scope,
        secret_sealed, dek_sealed, kek_id, has_refresh, access_expires_at, status, last_error, grant_id, connected_by, connected_at, refreshed_at)
      VALUES (${grant.plugin}, ${grant.plugin_url}, ${grant.resource}, ${grant.issuer}, ${grant.token_endpoint}, ${grant.client_id},
        'client_secret_post', 'configured', NULL, ${sealed.secretSealed}, ${sealed.dekSealed}, ${sealed.kekId}, true, NULL, 'connected', NULL,
        ${'00000000-0000-4000-8000-000000000001'}, 'test', now(), NULL)`
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  /** The in-process server a turn without Temporal calls, wired as main.ts wires `toolServices.secrets`. */
  async function turn(session: string) {
    const wired = harnessTools(
      services({ workspace: files, secrets: async () => [SAVED, ...(await readPluginSecrets({ kek, plugins, oauth }))] }),
    )
    const servers = wired.mcpServers({ id: session, owner: browser }, undefined, { secrets: () => [UNSAVED_HEADER] })
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await servers[SERVER_NAME]!.instance.connect(serverSide)
    const mcp = new Client({ name: 'turn-plugin-secrets-test', version: '0' })
    await mcp.connect(clientSide)
    return mcp
  }

  it("refuses a workspace file that holds an OAuth refresh token or client secret, or the turn's unsaved header", async () => {
    const written: unknown[] = []
    backend.use(
      http.put(`${BACKEND}/api/v1/models/box/readme`, async ({ request }) => {
        written.push(await request.json())
        return HttpResponse.json({ slug: 'box' })
      }),
    )
    const { session } = await m.start(browser, { origin: 'chat' })
    const ws = { kind: 'session' as const, sessionId: session.id }
    const mcp = await turn(session.id)
    for (const secret of [REFRESH, CLIENT_SECRET, UNSAVED_HEADER, SAVED]) {
      const file = await files.write(ws, 'README.md', Buffer.from(`# Box\n\ntoken: ${secret}\n`))
      const result = await mcp.callTool({ name: 'set_readme', arguments: { slug: 'box', workspace_path: 'README.md', workspace_sha256: file.sha256 } })
      const message = String(firstText(result as never))
      expect(result.isError, secret).toBe(true)
      expect(message).toMatch(/^set_readme: refused, .* contains one of the agent's credentials/)
      expect(message).not.toContain(secret)
    }
    expect(written).toEqual([])
    // Without a secret, the same upload goes.
    const clean = await files.write(ws, 'README.md', Buffer.from('# Box\n'))
    const ok = await mcp.callTool({ name: 'set_readme', arguments: { slug: 'box', workspace_path: 'README.md', workspace_sha256: clean.sha256 } })
    expect(ok.isError ?? false, JSON.stringify(ok)).toBe(false)
    expect(written).toEqual([{ content: '# Box\n' }])
    await mcp.close()
  })
})
