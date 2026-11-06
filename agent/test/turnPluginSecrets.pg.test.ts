import { createHash, randomBytes } from 'node:crypto'
import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { loadPackagesForRun, PackageInstaller } from '../src/plugins/packages/install.js'
import { validateSource } from '../src/plugins/packages/source.js'
import { grantAad, PluginOAuth } from '../src/plugins/oauthStore.js'
import { PluginStore } from '../src/plugins/registry.js'
import { toolSecrets } from '../src/plugins/secrets.js'
import { kekFromBase64, sealSecret } from '../src/secrets.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { harnessTools } from '../src/tools/harness.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { SessionFiles } from '../src/workspace/store.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { gitMissing, gitRepo, GREETER, localFetcher, resolver } from './support/gitRepo.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #5480: a turn without Temporal (the in-process harness, tools/harness.ts) checks an
// upload against the same list as a ToolCall and the UI's pick: the saved credentials and
// every plugin secret (plugins/secrets.ts `toolSecrets`, which main.ts wires as
// `toolServices.secrets`, #5483), with the turn's own loaded headers on top.

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
  let credentials: MemoryCredentials

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    files = new SessionFiles(db.sql)
    oauth = new PluginOAuth(db.sql)
    plugins = new PluginStore(db.sql, { oauth })
    credentials = new MemoryCredentials()
    await credentials.create({ kind: 'anthropic_api_key', secret: SAVED }, kek)
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
    const wired = harnessTools(services({ workspace: files, secrets: toolSecrets({ kek, credentials, plugins, oauth }).secrets }))
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

  // #5485: a package's header values are read from its pinned files, so a package that fails
  // to load still has them refused by an upload and by the pick, and redacted.
  it.skipIf(gitMissing !== undefined)('keeps a package that fails to load in the list: its header is refused and redacted', async () => {
    const TOKEN = 'pkg-header-token-77'
    const repo = gitRepo({
      ...GREETER,
      '.mcp.json': JSON.stringify({ mcpServers: { mem: { type: 'http', url: 'https://mcp.example/mcp/', headers: { Authorization: `Bearer ${TOKEN}` } } } }),
    })
    const cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-cache-'))
    try {
      const fetcher = localFetcher({ greeter: repo })
      const prepared = await new PackageInstaller({ fetcher, cacheRoot, resolve: resolver() }).prepare(
        validateSource({ kind: 'git', url: 'https://git.test/greeter.git' }),
      )
      const pin = { name: prepared.review.name, fetchUrl: prepared.fetchUrl, fetchPath: prepared.fetchPath, commit: prepared.commit, contentHash: prepared.contentHash }
      const store = { enabledPins: () => Promise.resolve([pin]) }
      // Its endpoint now fails the egress check, so no turn loads it.
      const installer = new PackageInstaller({ fetcher, cacheRoot, resolve: resolver({ 'mcp.example': ['169.254.169.254'] }) })
      const loaded = await loadPackagesForRun(store, installer)
      expect(loaded.loaded).toEqual([])
      expect(loaded.problems.join()).toMatch(/MCP server "mem"/)
      // As main.ts wires it.
      const agentSecrets = toolSecrets({ kek, credentials, plugins, oauth, packages: { store, installer } })

      const { session } = await m.start(browser, { origin: 'chat' })
      const ws = { kind: 'session' as const, sessionId: session.id }
      const body = `# Box\n\ntoken: ${TOKEN}\n`
      const file = await files.write(ws, 'README.md', Buffer.from(body))
      const wired = harnessTools(services({ workspace: files, secrets: agentSecrets.secrets }))
      const servers = wired.mcpServers({ id: session.id, owner: browser })
      const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
      await servers[SERVER_NAME]!.instance.connect(serverSide)
      const mcp = new Client({ name: 'turn-plugin-secrets-test', version: '0' })
      await mcp.connect(clientSide)

      // The upload is refused.
      const upload = await mcp.callTool({ name: 'set_readme', arguments: { slug: 'box', workspace_path: 'README.md', workspace_sha256: file.sha256 } })
      expect(upload.isError).toBe(true)
      expect(String(firstText(upload as never))).toMatch(/^set_readme: refused, .* contains one of the agent's credentials/)
      // workspace_read redacts it.
      const read = JSON.stringify(await mcp.callTool({ name: 'workspace_read', arguments: { path: 'README.md' } }))
      expect(read).toContain('[redacted]')
      expect(read).not.toContain(TOKEN)
      await mcp.close()

      // The pick is refused.
      const app = createApp({
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
        workspaceSecrets: agentSecrets.fresh,
      })
      const sha = createHash('sha256').update(body).digest('hex')
      const pick = await app.request(`/api/v1/ai/sessions/${session.id}/files/README.md?expected_sha256=${sha}`, {
        headers: { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' },
      })
      expect(pick.status).toBe(422)
    } finally {
      repo.remove()
      rmSync(cacheRoot, { recursive: true, force: true })
    }
  })
})
