import { describe, expect, it } from 'vitest'
import { aiPlugins } from '../../api/aiPlugins'
import { aiCapabilities } from '../../api/capabilities'
import type { ApiError } from '../../api/client'

describe('capabilities mock (#2171)', () => {
  it('seeds ten ScadBuddy tools and the other built-ins', async () => {
    const sources = await aiCapabilities.list()
    expect(sources.map((s) => s.id)).toEqual([
      'builtin:scadbuddy',
      'builtin:browser',
      'builtin:http',
      'builtin:own-plugin',
      'remote:hindsight',
    ])
    expect(sources[0]!.items).toHaveLength(10)
    expect(sources[1]).toMatchObject({ options_route: '/api/v1/ai/settings/browser-origins', items: [{ id: 'browser_click', default: 'write' }] })
  })

  it('lists a remote plugin after a refresh, recall at its stored read', async () => {
    expect((await aiCapabilities.source('remote:hindsight')).items.map((i) => [i.id, i.missing])).toEqual([['recall', true]])
    const { source } = await aiCapabilities.refresh('remote:hindsight')
    expect(source.listed_at).not.toBeNull()
    expect(source.items.find((i) => i.id === 'recall')).toMatchObject({ access: 'read', overridden: true, suggested: 'read', missing: false })
    expect(source.items.find((i) => i.id === 'files.list')).toMatchObject({ allowed: ['off', 'outward'] })
  })

  it('writes remote access into the plugins state, and reads plugins registered there', async () => {
    await aiCapabilities.refresh('remote:hindsight')
    await aiCapabilities.setItem('remote:hindsight', 'retain', 'off')
    await aiCapabilities.setEnabled('remote:hindsight', true)
    const [listed] = (await aiPlugins.listPlugins()).filter((p) => p.name === 'hindsight')
    expect(listed).toMatchObject({ enabled: true, disabled_tools: ['retain'] })

    // The default resets.
    const after = await aiCapabilities.setItem('remote:hindsight', 'recall', 'outward')
    expect(after.items.find((i) => i.id === 'recall')).toMatchObject({ access: 'outward', overridden: false })

    await aiPlugins.createRemote({ name: 'notes', url: 'https://notes.example/mcp/' })
    expect(await aiCapabilities.source('remote:notes')).toMatchObject({ items: [], listed_at: null })
    await aiPlugins.deleteRemote('notes')
    await expect(aiCapabilities.source('remote:notes')).rejects.toMatchObject({ status: 404 })
  })

  it('refuses as the agent does', async () => {
    await aiCapabilities.refresh('remote:hindsight')
    const refusal = (run: Promise<unknown>) => run.catch((e: unknown) => (e as ApiError).status)
    expect(await refusal(aiCapabilities.setItem('remote:hindsight', 'files.list', 'read'))).toBe(409)
    expect(await refusal(aiCapabilities.setItem('builtin:scadbuddy', 'list_models', 'on'))).toBe(400)
    expect(await refusal(aiCapabilities.setItem('builtin:scadbuddy', 'nope', 'off'))).toBe(404)
    expect(await refusal(aiCapabilities.refresh('builtin:http'))).toBe(409)
    const http = await aiCapabilities.setItem('builtin:http', 'http_request', 'read')
    expect(http.items[0]).toMatchObject({ access: 'read', overridden: true })
    expect((await aiCapabilities.resetItem('builtin:http', 'http_request')).items[0]).toMatchObject({ access: null, overridden: false })
  })
})
