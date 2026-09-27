import { describe, expect, it } from 'vitest'
import { ApiError, api } from '../api/client'
import { BUILTIN_SLUG, versionIds } from './fixtures'

const refusal = `'${BUILTIN_SLUG}' is a built-in template and is read-only`

describe('mock API: a built-in template (#192)', () => {
  it.each([
    ['delete', () => api.deleteModel(BUILTIN_SLUG)],
    ['replace the source of', () => api.replaceSource(BUILTIN_SLUG, 'cube(1);\n')],
    ['restore a revision of', () => api.restoreVersion(BUILTIN_SLUG, versionIds.builtinFirst)],
  ])('refuses to %s one with the 403 require_mine answers', async (_, write) => {
    const error: unknown = await write().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 403, detail: refusal })
  })

  it('refuses a metadata edit the same way', async () => {
    const response = await fetch(`/api/v1/models/${encodeURIComponent(BUILTIN_SLUG)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Mine now' }),
    })
    expect(response.status).toBe(403)
    expect(response.headers.get('Content-Type')).toBe('application/problem+json')
    expect(await response.json()).toMatchObject({ status: 403, detail: refusal })
  })

  it('still serves every read', async () => {
    expect((await api.getModel(BUILTIN_SLUG)).origin).toBe('builtin')
    expect(await api.listVersions(BUILTIN_SLUG)).toHaveLength(2)
    expect((await api.getSchema(BUILTIN_SLUG)).title).toBe('Keychain Template')
    expect(await api.getSource(BUILTIN_SLUG)).toContain('/* [Text] */')
  })
})
