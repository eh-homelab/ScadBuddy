import { describe, expect, it } from 'vitest'
import { ok } from '../src/tools/call.js'
import { ToolError } from '../src/tools/registry.js'

// #5918 review N1: `ok` keeps a problem's slug only for ScadBuddy's own problem types.

async function refusalOf(error: unknown, status = 409): Promise<ToolError> {
  try {
    await ok(Promise.resolve({ error, response: new Response(null, { status }) }), 'claim')
  } catch (err) {
    if (err instanceof ToolError) return err
    throw err
  }
  throw new Error('ok did not refuse')
}

describe('ok keeps a ScadBuddy problem slug', () => {
  it('takes the last segment of a ScadBuddy problem type', async () => {
    expect((await refusalOf({ type: 'https://scadbuddy.dev/problems/orders/already_notified', detail: 'x' })).problem).toBe('already_notified')
  })

  it('keeps a hyphenated slug whole', async () => {
    expect((await refusalOf({ type: 'https://scadbuddy.dev/problems/storefront/draft-body-invalid', detail: 'x' })).problem).toBe('draft-body-invalid')
  })

  it('leaves it out for about:blank, another host, or no type', async () => {
    expect((await refusalOf({ type: 'about:blank', detail: 'x' })).problem).toBeUndefined()
    expect((await refusalOf({ type: 'https://example.com/problems/orders/already_notified', detail: 'x' })).problem).toBeUndefined()
    expect((await refusalOf({ detail: 'x' })).problem).toBeUndefined()
  })
})
