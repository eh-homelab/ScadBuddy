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

describe('mock API: duplicate (#156)', () => {
  it('answers 201 with a template of mine recording its upstream', async () => {
    const response = await fetch(`/api/v1/models/${encodeURIComponent(BUILTIN_SLUG)}/duplicate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'My Keychain' }),
    })
    expect(response.status).toBe(201)
    expect(await response.json()).toMatchObject({
      slug: 'my-keychain',
      name: 'My Keychain',
      origin: 'mine',
      upstream: { id: BUILTIN_SLUG, path: '_builtin/keychain-template', base: versionIds.synced },
    })
    expect(await api.getSource('my-keychain')).toContain('/* [Text] */')
    expect((await api.listVersions('my-keychain'))[0]?.message).toBe(
      `Duplicate ${BUILTIN_SLUG} as my-keychain`,
    )
  })

  it('refuses a slug that is taken with a 409', async () => {
    const error: unknown = await api
      .duplicateModel(BUILTIN_SLUG, 'Name Keychain')
      .catch((caught: unknown) => caught)
    expect(error).toMatchObject({
      status: 409,
      detail: "a model named 'name-keychain' already exists",
    })
  })

  it('answers 404 for a template that is not there', async () => {
    const error: unknown = await api
      .duplicateModel('gone', 'Copy')
      .catch((caught: unknown) => caught)
    expect(error).toMatchObject({ status: 404 })
  })
})

describe('mock API: delete a template duplicates track (#223)', () => {
  it('answers 409 naming them, and deletes with force', async () => {
    await api.duplicateModel('name-keychain', 'My Keychain')

    const error: unknown = await api.deleteModel('name-keychain').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, problem: { duplicates: 1, slugs: ['my-keychain'] } })
    expect((await api.getModel('name-keychain')).slug).toBe('name-keychain')

    await api.deleteModel('name-keychain', true)
    const gone: unknown = await api.getModel('name-keychain').catch((caught: unknown) => caught)
    expect(gone).toMatchObject({ status: 404 })
  })
})
