import { describe, expect, it } from 'vitest'
import { createApp, type Health } from '../src/app.js'

const up = () => Promise.resolve(true)
const down = () => Promise.resolve(false)

async function health(app: ReturnType<typeof createApp>): Promise<{ status: number; body: Health }> {
  const res = await app.request('/healthz')
  return { status: res.status, body: (await res.json()) as Health }
}

describe('GET /healthz', () => {
  it('reports AI disabled when no database is configured', async () => {
    const { status, body } = await health(createApp({ database: undefined, backend: up }))
    expect(status).toBe(200)
    expect(body).toEqual({
      status: 'ok',
      ai: 'disabled (no database)',
      database: 'not configured',
      backend: 'ok',
    })
  })

  it('reports AI enabled when the database answers', async () => {
    const { body } = await health(createApp({ database: { ping: up }, backend: up }))
    expect(body.ai).toBe('enabled')
    expect(body.database).toBe('ok')
  })

  it('stays 200 and says why when the database or backend is down', async () => {
    const { status, body } = await health(createApp({ database: { ping: down }, backend: down }))
    expect(status).toBe(200)
    expect(body).toEqual({
      status: 'ok',
      ai: 'unavailable (database unreachable)',
      database: 'unreachable',
      backend: 'unreachable',
    })
  })

  it('has no other routes yet', async () => {
    const res = await createApp({ database: undefined, backend: up }).request('/api/v1/ai/chat')
    expect(res.status).toBe(404)
  })
})
