import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { aiPlugins, refusalProblems } from './aiPlugins'
import { printRunPoll } from './client'

// Installing and re-pinning a plugin package are the agent's commands (#1055): one
// Idempotency-Key per press, and a 202 followed at the agent's own
// /api/v1/ai/operations/{id}, not the backend's.

const operation = {
  id: 'aop-1',
  kind: 'plugin_package_install',
  subject: 'git.test',
  status: 'running',
  result: null,
  error: null,
  created_at: '2026-10-04T00:00:00Z',
  finished_at: null,
}
const pkg = { name: 'greeter' }
const source = { kind: 'git' as const, url: 'https://git.test/greeter.git' }
const saved = { ...printRunPoll }

afterEach(() => Object.assign(printRunPoll, saved))

describe('plugin package commands', () => {
  it('sends an Idempotency-Key and follows a 202 at the agent', async () => {
    printRunPoll.intervalMs = 1
    let key: string | null = null
    let reads = 0
    server.use(
      http.post('/api/v1/ai/plugin-packages', ({ request }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json(operation, { status: 202 })
      }),
      http.get('/api/v1/ai/operations/aop-1', () => {
        reads += 1
        return HttpResponse.json(reads < 2 ? operation : { ...operation, status: 'succeeded', result: pkg })
      }),
    )
    await expect(aiPlugins.installPackage(source)).resolves.toEqual(pkg)
    expect(key).toMatch(/^[0-9a-f]{32}$/)
  })

  it("keeps a refused install's problems, answered at once or by the operation", async () => {
    printRunPoll.intervalMs = 1
    server.use(
      http.post('/api/v1/ai/plugin-packages', () =>
        HttpResponse.json({ detail: 'the plugin package is refused', problems: ['a hook runs a command'] }, { status: 422 }),
      ),
    )
    expect(refusalProblems(await aiPlugins.installPackage(source).catch((e: unknown) => e))).toEqual([
      'a hook runs a command',
    ])
    const error = {
      status: 422,
      title: 'Unprocessable Content',
      detail: 'the plugin package is refused',
      extensions: { problems: ['late'] },
    }
    server.use(
      http.post('/api/v1/ai/plugin-packages/greeter/repin', () => HttpResponse.json(operation, { status: 202 })),
      http.get('/api/v1/ai/operations/aop-1', () => HttpResponse.json({ ...operation, status: 'failed', error })),
    )
    expect(refusalProblems(await aiPlugins.repinPackage('greeter').catch((e: unknown) => e))).toEqual(['late'])
  })
})
