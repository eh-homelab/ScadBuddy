import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { AuthorizeInput, grantable } from '../src/harness/headlessGrants.js'

// A grant covers method and path, not the body, so settings routes can never be
// granted (review of #518): approving PUT /api/v1/settings would let the page
// point bambuddy_url anywhere. The backend's `grantable` is the same rule
// (backend/tests/api/test_agent_actor_grants.py).

const schema = z.object(AuthorizeInput)

describe('authorize_request input', () => {
  it('refuses settings routes, with or without a trailing slash', () => {
    for (const path of ['/api/v1/settings', '/api/v1/settings/', '/api/v1/settings/print-options']) {
      expect(grantable(path)).toBe(false)
      expect(schema.safeParse({ method: 'PUT', path }).success).toBe(false)
    }
  })

  it('accepts an outward route, and one that only starts like settings', () => {
    for (const path of ['/api/v1/print/outputs/out-1/run', '/api/v1/settingsx']) {
      expect(grantable(path)).toBe(true)
      expect(schema.safeParse({ method: 'POST', path }).success).toBe(true)
    }
  })
})
