import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { route } from '../src/routes/templates.js'
import { BLANK_TEMPLATE } from '../src/tools/templates.js'
import { baseDeps } from './helpers/mcp.js'

// GET /api/v1/ai/templates/blank (#1291): New model's "Start from blank template"
// gets the very string the agent's create_from_template writes.

describe('GET /api/v1/ai/templates/blank', () => {
  it('answers the blank template', async () => {
    const app = new Hono()
    route.register(app, baseDeps({}), new AbortController().signal)
    const res = await app.request('https://scadbuddy.example/api/v1/ai/templates/blank')
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ source: BLANK_TEMPLATE })
  })
})
