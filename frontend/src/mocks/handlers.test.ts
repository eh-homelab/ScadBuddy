import { describe, expect, it } from 'vitest'
import { ApiError, api } from '../api/client'
import { COPY, UPSTREAM, duplicateWithUpdate, ours, theirs } from '../test/upstream'
import { BUILTIN_SLUG, keychainSource, versionIds } from './fixtures'

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

describe('mock API: upstream updates (#157)', () => {
  it('reports a duplicate current, then an update once its upstream moves', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    expect((await api.getUpstream(COPY)).state).toBe('current')
    expect((await api.getModel(COPY)).upstream_state).toBe('current')

    await api.replaceSource(UPSTREAM, theirs)
    expect(await api.getUpstream(COPY)).toMatchObject({
      state: 'update',
      revision: (await api.listVersions(UPSTREAM))[0]?.commit,
      preview: { ours: keychainSource, base: keychainSource, theirs, merged: theirs, clean: true },
    })
    expect((await api.listModels()).find((m) => m.slug === COPY)?.upstream_state).toBe('update')
  })

  it('serves upstream_state on a metadata edit, as on every other write', async () => {
    await duplicateWithUpdate()
    expect((await api.updateModel(COPY, { description: 'Mine' })).upstream_state).toBe('update')
  })

  it('answers 404 for a template that is not a duplicate', async () => {
    const error: unknown = await api.getUpstream(UPSTREAM).catch((caught: unknown) => caught)
    expect(error).toMatchObject({
      status: 404,
      detail: `'${UPSTREAM}' is not a duplicate, so it has no upstream`,
    })
  })

  it('answers a conflicted merge with 409, the marked-up source and merge_base', async () => {
    await duplicateWithUpdate({ conflict: true })
    const revision = (await api.listVersions(UPSTREAM))[0]?.commit
    const error: unknown = await api.mergeUpstream(COPY).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    const { problem } = error as ApiError
    expect(problem).toMatchObject({ status: 409, merge_base: revision, conflicts: 1 })
    expect(problem['merged']).toContain('<<<<<<< ')
    expect(await api.getSource(COPY)).toBe(ours)
  })

  it('refuses a resolution that still has markers, or names no upstream revision', async () => {
    await duplicateWithUpdate({ conflict: true })
    const revision = (await api.listVersions(UPSTREAM))[0]?.commit ?? ''
    const marked: unknown = await api
      .resolveUpstreamMerge(COPY, '<<<<<<< ours\ncube(1);\n', revision)
      .catch((caught: unknown) => caught)
    expect(marked).toMatchObject({ status: 422 })
    const stranger: unknown = await api
      .resolveUpstreamMerge(COPY, 'cube(1);\n', versionIds.synced)
      .catch((caught: unknown) => caught)
    expect(stranger).toMatchObject({ status: 422 })

    await api.resolveUpstreamMerge(COPY, 'cube(1);\n', revision)
    expect((await api.getModel(COPY)).upstream_state).toBe('current')
  })

  it('dismisses an update until the upstream moves again', async () => {
    await duplicateWithUpdate()
    expect((await api.dismissUpstream(COPY)).upstream_state).toBe('dismissed')
    await api.replaceSource(UPSTREAM, `${theirs}// again\n`)
    expect((await api.getModel(COPY)).upstream_state).toBe('update')
  })

  it('refuses to dismiss with no update, and to detach while the upstream exists', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    for (const action of [() => api.dismissUpstream(COPY), () => api.detachUpstream(COPY)]) {
      const error: unknown = await action().catch((caught: unknown) => caught)
      expect(error).toMatchObject({ status: 409 })
    }
  })

  it('reports a deleted upstream gone, and detaches from it', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    await api.deleteModel(UPSTREAM, true)
    expect((await api.getUpstream(COPY)).state).toBe('gone')
    expect((await api.detachUpstream(COPY)).upstream).toBeNull()
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
