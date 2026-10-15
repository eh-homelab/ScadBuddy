import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { type BuiltinResult, GREP_BUDGET_MS } from '../src/builtins/files.js'
import { BUILTIN_SERVER, builtinServer, type BuiltinContext } from '../src/builtins/server.js'
import { TASKS_PATH } from '../src/builtins/session.js'
import { type NestedQueries, webFetch } from '../src/builtins/web.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { CALL_ID_META } from '../src/builtins/callId.js'
import { connectInProcess } from '../src/temporal/turnTools.js'
import { SessionFiles, type WorkspaceRef } from '../src/workspace/store.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #2393: an allowed package's Claude Code built-ins in a durable turn, as the `builtin`
// turn tools: the file tools on the session's workspace, the task list, WebFetch through
// the egress-checked exchange, each answering in Claude Code's words (measured against
// the bundled Claude Code 2.1.296 in a classic turn; see the expectations).

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const ALL = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'NotebookEdit', 'WebFetch', 'WebSearch', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate', 'ReportFindings', 'EnterWorktree', 'ExitWorktree', 'SendMessage']

type Content = { type: string; text?: string; data?: string; mimeType?: string }

describe.skipIf(skip !== undefined)(`a durable turn's built-ins${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let files: SessionFiles
  let ws: WorkspaceRef
  let client: Client
  const asked: string[] = []
  const searched: unknown[] = []
  const nested: NestedQueries = {
    ask: async (prompt) => {
      asked.push(prompt)
      return 'the answer'
    },
    search: async (input): Promise<BuiltinResult> => {
      searched.push(input)
      return { text: 'Web search results for query: "openscad"' }
    },
  }
  const servers: Server[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    files = new SessionFiles(db.sql)
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    const { session } = await m.start(browser, { origin: 'chat' })
    ws = { kind: 'session', sessionId: session.id }
    const context = (): BuiltinContext => ({
      files: { files, ws },
      web: { nested, secrets: () => ['sk-secret-token'], actor: browser, sessionId: session.id, signal: new AbortController().signal, resolve: async () => ['127.0.0.1'], allowLoopback: true },
    })
    client = await connectInProcess(() => builtinServer(ALL, context))
    asked.length = 0
    searched.length = 0
  })
  afterEach(async () => {
    await client.close()
    await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))))
    await drop()
  })

  async function call(name: string, args: Record<string, unknown>): Promise<{ text: string; isError: boolean; content: Content[] }> {
    const result = (await client.callTool({ name, arguments: args })) as { content: Content[]; isError?: boolean }
    return { text: result.content[0]?.text ?? '', isError: result.isError === true, content: result.content }
  }

  it("lists Claude Code's names with its input schemas", async () => {
    const { tools } = await client.listTools()
    expect(tools.map((t) => t.name)).toEqual(ALL)
    expect(tools.find((t) => t.name === 'Read')?.inputSchema).toMatchObject({ required: ['file_path'] })
    expect(BUILTIN_SERVER).toBe('builtin')
  })

  it('writes, reads, edits and finds files in the workspace, in Claude Code\'s words', async () => {
    expect(await call('Write', { file_path: '/workspace/a.txt', content: 'hello\nworld\nhello again\n' })).toMatchObject({
      text: 'File created successfully at: /workspace/a.txt',
      isError: false,
    })
    expect((await call('Read', { file_path: '/workspace/a.txt' })).text).toBe('1\thello\n2\tworld\n3\thello again\n4\t')
    expect((await call('Read', { file_path: '/workspace/a.txt', offset: 2, limit: 1 })).text).toBe('2\tworld')
    expect(await call('Read', { file_path: '/workspace/missing.txt' })).toEqual(
      expect.objectContaining({ text: 'File does not exist. Note: your current working directory is /workspace.', isError: true }),
    )
    expect(await call('Read', { file_path: '/etc/passwd' })).toMatchObject({ isError: true, text: expect.stringContaining('outside the session workspace') })
    expect((await call('Glob', { pattern: '*.txt' })).text).toBe('a.txt')
    expect((await call('Grep', { pattern: 'hello' })).text).toBe('Found 1 file\na.txt')
    expect((await call('Grep', { pattern: 'hello', output_mode: 'content', '-n': true })).text).toBe('a.txt:1:hello\na.txt:3:hello again')
    expect((await call('Grep', { pattern: 'hello', output_mode: 'count' })).text).toBe('a.txt:2\n\nFound 2 total occurrences across 1 file.')
    expect(await call('Edit', { file_path: '/workspace/a.txt', old_string: 'world', new_string: 'there' })).toMatchObject({
      text: 'The file /workspace/a.txt has been updated successfully.',
      isError: false,
    })
    expect(await call('Edit', { file_path: '/workspace/a.txt', old_string: 'hello', new_string: 'x' })).toMatchObject({
      isError: true,
      text: expect.stringContaining('Found 2 matches of the string to replace, but replace_all is false.'),
    })
    expect(await call('Write', { file_path: 'a.txt', content: 'over\n' })).toMatchObject({ text: 'The file /workspace/a.txt has been updated successfully.' })
    expect((await files.read(ws, 'a.txt'))!.bytes.toString()).toBe('over\n')
    // The workspace_* tools see the same files.
    await files.write(ws, 'img/p.png', PNG)
    const image = await call('Read', { file_path: '/workspace/img/p.png' })
    expect(image.content[1]).toMatchObject({ type: 'image', mimeType: 'image/png', data: PNG.toString('base64') })
    expect(await call('Write', { file_path: '/workspace/../x', content: '' })).toMatchObject({ isError: true })
  })

  it('stops a pattern that backtracks without end, and answers in time', async () => {
    await files.write(ws, 'slow.txt', Buffer.from(`${'a'.repeat(40)}b\n`))
    const started = Date.now()
    expect(await call('Grep', { pattern: '(a+)+$', output_mode: 'content' })).toMatchObject({ isError: true, text: expect.stringContaining('took longer than') })
    expect(Date.now() - started).toBeLessThan(GREP_BUDGET_MS + 2_000)
  })

  it('edits a notebook cell', async () => {
    const nb = { cells: [{ cell_type: 'code', id: 'c1', source: ['print(1)'], metadata: {}, outputs: [], execution_count: 3 }], metadata: {}, nbformat: 4, nbformat_minor: 5 }
    await files.write(ws, 'nb.ipynb', Buffer.from(JSON.stringify(nb)))
    expect((await call('NotebookEdit', { notebook_path: '/workspace/nb.ipynb', cell_id: 'c1', new_source: 'print(2)' })).text).toBe('Updated cell c1 with print(2)')
    const after = JSON.parse((await files.read(ws, 'nb.ipynb'))!.bytes.toString()) as typeof nb
    expect(after.cells[0]).toMatchObject({ source: 'print(2)', execution_count: null, outputs: [] })
  })

  it("keeps the task list across calls, as Claude Code's does within a turn", async () => {
    expect((await call('TaskList', {})).text).toBe('No tasks found')
    expect((await call('TaskCreate', { subject: 's', description: 'd', activeForm: 'Doing s' })).text).toBe('Task #1 created successfully: s')
    expect((await call('TaskCreate', { subject: 't', description: 'e' })).text).toBe('Task #2 created successfully: t')
    expect((await call('TaskGet', { taskId: '1' })).text).toBe('Task #1: s\nStatus: pending\nDescription: d')
    expect((await call('TaskUpdate', { taskId: '1', status: 'in_progress', addBlocks: ['2'] })).text).toBe('Updated task #1 status, blocks')
    expect((await call('TaskUpdate', { taskId: '2', status: 'completed' })).text).toBe('Updated task #2 status')
    expect((await call('TaskList', {})).text).toBe('#1 [in_progress] s\n#2 [completed] t [blocked by #1]')
    expect((await call('TaskGet', { taskId: '2' })).text).toBe('Task #2: t\nStatus: completed\nDescription: e\nBlocked by: #1')
    expect((await call('TaskUpdate', { taskId: '1', status: 'deleted' })).text).toBe('Updated task #1 deleted')
    expect((await call('TaskList', {})).text).toBe('#2 [completed] t')
    expect((await call('TaskGet', { taskId: '9' })).text).toBe('Task not found')
    expect(await files.stat(ws, TASKS_PATH)).toBeDefined()
  })

  it('answers every Task* call with an error naming the list file when it is not a task list (#3201)', async () => {
    for (const bad of ['{not json', '{}', '{"next":1,"tasks":{}}', '[]']) {
      await files.write(ws, TASKS_PATH, Buffer.from(bad))
      for (const [name, args] of [
        ['TaskList', {}],
        ['TaskGet', { taskId: '1' }],
        ['TaskCreate', { subject: 's', description: 'd' }],
        ['TaskUpdate', { taskId: '1', status: 'completed' }],
      ] as const) {
        expect(await call(name, args), `${name} on ${bad}`).toMatchObject({ isError: true, text: expect.stringContaining(`/workspace/${TASKS_PATH}`) })
      }
      expect((await files.read(ws, TASKS_PATH))!.bytes.toString()).toBe(bad)
    }
  })

  it('creates one task for a TaskCreate its activity retries, by the call id in _meta (#3201)', async () => {
    const create = (callId: string) =>
      client.callTool({ name: 'TaskCreate', arguments: { subject: 's', description: 'd' }, _meta: { [CALL_ID_META]: callId } }) as Promise<{ content: Content[] }>
    expect((await create('toolu_a')).content[0]?.text).toBe('Task #1 created successfully: s')
    expect((await create('toolu_a')).content[0]?.text).toBe('Task #1 created successfully: s')
    expect((await create('toolu_b')).content[0]?.text).toBe('Task #2 created successfully: s')
    expect((await call('TaskList', {})).text).toBe('#1 [pending] s\n#2 [pending] s')
  })

  it('answers the built-ins with nothing to act on as a classic turn does', async () => {
    expect((await call('ReportFindings', { findings: [] })).text).toBe('No findings reported.')
    expect((await call('ReportFindings', { findings: [{}, {}] })).text).toBe('2 findings reported.')
    expect(await call('EnterWorktree', { name: 'w' })).toMatchObject({ isError: true, text: expect.stringContaining('not in a git repository') })
    expect(JSON.parse((await call('SendMessage', { to: 'someone', message: 'hi' })).text)).toMatchObject({ success: false })
  })

  it('fetches a page through the egress check, as markdown, and asks the small model about it', async () => {
    const pages = createServer((req, res) => {
      if (req.url === '/moved') {
        res.writeHead(302, { location: 'http://elsewhere.test/page' })
        res.end()
        return
      }
      res.writeHead(200, { 'content-type': 'text/html' })
      res.end('<html><head><script>alert(1)</script></head><body><h1>Title</h1><p>Some <b>bold</b> text.</p></body></html>')
    })
    servers.push(pages)
    await new Promise<void>((r) => pages.listen(0, '127.0.0.1', r))
    const port = (pages.address() as AddressInfo).port
    const result = await call('WebFetch', { url: `http://pages.test:${port}/`, prompt: 'What is the title?' })
    expect(result.isError).toBe(false)
    expect(unwrapUntrusted(result.text)).toBe('the answer')
    expect(asked[0]).toContain('# Title\n\nSome **bold** text.')
    expect(asked[0]).not.toContain('alert(1)')
    expect(asked[0]).toContain('What is the title?')
    expect((await call('WebFetch', { url: `http://pages.test:${port}/moved`, prompt: 'p' })).text).toContain('REDIRECT DETECTED')
    expect(await call('WebFetch', { url: 'http://localhost/', prompt: 'p' })).toMatchObject({ isError: true })
    expect(asked).toHaveLength(1)
    expect(await call('WebFetch', { url: `http://pages.test:${port}/?k=sk-secret-token`, prompt: 'p' })).toMatchObject({ isError: true })
    expect(asked).toHaveLength(1)
  })

  it("refuses the pod's loopback, by literal or by what a name resolves to", async () => {
    const web = { nested, secrets: () => [], actor: browser, signal: new AbortController().signal }
    expect(await webFetch({ ...web, resolve: async () => ['93.184.215.14'] }, { url: 'http://127.0.0.1:9/', prompt: 'p' })).toMatchObject({
      isError: true,
      text: expect.stringContaining('loopback'),
    })
    expect(await webFetch({ ...web, resolve: async () => ['127.0.0.1'] }, { url: 'http://rebound.example/', prompt: 'p' })).toMatchObject({
      isError: true,
      text: expect.stringContaining('loopback'),
    })
    expect(asked).toEqual([])
  })

  it('hands a search to the nested query as it was asked', async () => {
    expect((await call('WebSearch', { query: 'openscad' })).text).toBe('Web search results for query: "openscad"')
    expect(searched).toEqual([{ query: 'openscad' }])
  })
})
