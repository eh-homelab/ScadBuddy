import { afterEach, describe, expect, it, vi } from 'vitest'
import { decodeEvent, MemoryEventSource } from '../src/events/bus.js'
import { canonical, expand, isTemplate, matchUri, RESOURCES, tierFor } from '../src/resources/catalog.js'
import { affectedBy } from '../src/resources/events.js'
import { ResourceHub, SubscriptionLimitError, Subscriptions } from '../src/resources/hub.js'
import { toContents } from '../src/resources/server.js'
import { ALL_TOOLS } from '../src/tools/index.js'

// Unit tests for the `scadbuddy://` resources (#264): the URI catalogue, the
// event → URI mapping, coalescing, and tool result → resource contents. The
// end-to-end MCP tests are test/mcpResources.test.ts (msw backend, in-memory
// bus) and test/mcpResources.pg.test.ts (the real Postgres bus).

const tools = new Map(ALL_TOOLS.map((t) => [t.name, t]))

describe('the resource catalogue', () => {
  it('backs every resource with a read tool from the registry', () => {
    for (const def of RESOURCES) {
      const tool = tools.get(def.tool)
      expect(tool, def.template).toBeDefined()
      expect(tool!.risk, def.template).toBe('read')
    }
  })

  it('needs the write tier for settings and read for everything else', () => {
    for (const def of RESOURCES) {
      expect(tierFor(def, tools.get(def.tool)!), def.template).toBe(def.name === 'settings' ? 'write' : 'read')
    }
  })

  it('has unique templates and names', () => {
    expect(new Set(RESOURCES.map((d) => d.template)).size).toBe(RESOURCES.length)
    expect(new Set(RESOURCES.map((d) => d.name)).size).toBe(RESOURCES.length)
  })

  it('matches URIs to their template and decodes variables', () => {
    expect(matchUri('scadbuddy://models/keychain/source')).toMatchObject({
      def: { name: 'model-source' },
      vars: { slug: 'keychain' },
    })
    expect(matchUri('scadbuddy://models/k/versions/abc1234/diff')).toMatchObject({
      def: { name: 'model-version-diff' },
      vars: { slug: 'k', commit: 'abc1234' },
    })
    expect(matchUri('scadbuddy://outputs/o1/plates/2/thumbnail')?.vars).toEqual({ output_id: 'o1', plate: '2' })
    expect(matchUri('scadbuddy://models')?.def.name).toBe('models')
  })

  it('refuses URIs that name no resource', () => {
    for (const uri of [
      'scadbuddy://nope',
      'scadbuddy://models/a/b/c',
      'scadbuddy://models/',
      'https://example.com/models',
      'scadbuddy://models/%E0%A4%A',
    ]) {
      expect(matchUri(uri), uri).toBeUndefined()
    }
  })

  it('percent-encodes on expansion, so a builtin slug has one canonical spelling', () => {
    const uri = expand('scadbuddy://models/{slug}', { slug: 'builtin:gridfinity-bin' })
    expect(uri).toBe('scadbuddy://models/builtin%3Agridfinity-bin')
    expect(canonical('scadbuddy://models/builtin:gridfinity-bin')).toBe(uri)
    expect(canonical(uri)).toBe(uri)
    expect(matchUri(uri)?.vars.slug).toBe('builtin:gridfinity-bin')
  })

  it('maps template variables to tool arguments', () => {
    const schema = RESOURCES.find((d) => d.name === 'model-version-schema')!
    expect(schema.args!({ slug: 'k', commit: 'abc1234' })).toEqual({ slug: 'k', version: 'abc1234' })
    const plate = RESOURCES.find((d) => d.name === 'output-plate-thumbnail')!
    expect(plate.args!({ output_id: 'o', plate: '3' })).toEqual({ output_id: 'o', plate: 3 })
    // Not a number: left as it is, so the tool's own validation refuses it.
    expect(plate.args!({ output_id: 'o', plate: 'x' })).toEqual({ output_id: 'o', plate: 'x' })
  })

  it('every URI the event mapping produces is a resource, in canonical form', () => {
    const events = [
      { kind: 'job.running', job_id: 'j', slug: 's' },
      { kind: 'job.done', job_id: 'j', slug: 'builtin:s' },
      { kind: 'model.created', slug: 'builtin:s' },
      { kind: 'model.updated', slug: 's' },
      { kind: 'model.deleted', slug: 's' },
      { kind: 'source.changed', slug: 's' },
      { kind: 'version.committed', slug: 's', commit: 'c' },
      { kind: 'upstream.available', slug: 's', upstream: 'u', commit: 'c' },
      { kind: 'output.created', slug: 's', output_id: 'o' },
      { kind: 'output.deleted', slug: 's', output_id: 'o' },
      { kind: 'print.progress', slug: 's', output_id: 'o' },
      { kind: 'print.settled', slug: 's', output_id: 'o' },
      { kind: 'library.changed', slug: 's', name: 'BOSL2' },
      { kind: 'library.removed', name: 'BOSL2', commits: [] },
      { kind: 'font.installed', family: 'Lobster Two' },
      { kind: 'settings.changed', section: 'connection' },
      { kind: 'session.started', session_id: '0e5a3c1e-1111-4222-8333-944455556666', seq: 2 },
      { kind: 'session.message', session_id: '0e5a3c1e-1111-4222-8333-944455556666', seq: 3 },
    ]
    for (const e of events) {
      const { uris } = affectedBy({ id: 'x', ...e })
      expect(uris.length, e.kind).toBeGreaterThan(0)
      for (const uri of uris) expect(canonical(uri), `${e.kind} → ${uri}`).toBe(uri)
    }
  })
})

describe('event → resource mapping', () => {
  it('a job update touches the job; a settled job also the model diagnostics', () => {
    expect(affectedBy({ id: '1', kind: 'job.running', job_id: 'j1', slug: 'k' })).toEqual({
      uris: ['scadbuddy://jobs/j1'],
      listChanged: false,
    })
    expect(affectedBy({ id: '1', kind: 'job.done', job_id: 'j1', slug: 'k' }).uris).toEqual([
      'scadbuddy://jobs/j1',
      'scadbuddy://models/k/diagnostics',
    ])
  })

  it('a model created or deleted changes the list; an update does not', () => {
    expect(affectedBy({ id: '1', kind: 'model.created', slug: 'k' }).listChanged).toBe(true)
    expect(affectedBy({ id: '1', kind: 'model.deleted', slug: 'k' }).listChanged).toBe(true)
    expect(affectedBy({ id: '1', kind: 'model.updated', slug: 'k' }).listChanged).toBe(false)
  })

  it('print progress touches the print progress resource', () => {
    expect(affectedBy({ id: '1', kind: 'print.progress', output_id: 'o1', slug: 'k' }).uris).toEqual([
      'scadbuddy://print/outputs/o1/progress',
    ])
  })

  it("a session's events touch it, and the list only when it moves in it (#300)", () => {
    const session_id = '0e5a3c1e-1111-4222-8333-944455556666'
    const one = `scadbuddy://sessions/${session_id}`
    expect(affectedBy({ id: '1', kind: 'session.message', session_id, seq: 9 })).toEqual({ uris: [one], listChanged: false })
    for (const kind of ['session.started', 'session.owner', 'session.waiting', 'session.done']) {
      expect(affectedBy({ id: '1', kind, session_id, seq: 9 }), kind).toEqual({
        uris: [one, 'scadbuddy://sessions'],
        listChanged: false,
      })
    }
    expect(affectedBy({ id: '1', kind: 'session.done' })).toEqual({ uris: [], listChanged: false })
  })

  it('ignores kinds it does not know and events missing their ids', () => {
    expect(affectedBy({ id: '1', kind: 'session.updated' })).toEqual({ uris: [], listChanged: false })
    expect(affectedBy({ id: '1', kind: 'job.running' })).toEqual({ uris: [], listChanged: false })
  })

  it('decodes the backend wire form, and refuses what is not an event', () => {
    const wire = JSON.stringify({ id: 'e1', at: '2026-09-28T00:00:00Z', kind: 'job.done', job_id: 'j', slug: 's' })
    expect(decodeEvent(wire)).toMatchObject({ id: 'e1', kind: 'job.done', job_id: 'j' })
    expect(decodeEvent({ id: 'e2', kind: 'library.removed', name: 'x', commits: ['a'] })).toMatchObject({ commits: ['a'] })
    expect(decodeEvent('not json')).toBeUndefined()
    expect(decodeEvent('{"kind":"job.done"}')).toBeUndefined()
    expect(decodeEvent(42)).toBeUndefined()
  })
})

describe('subscriptions and coalescing', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  function recorder() {
    const sent: string[] = []
    return {
      sent,
      notify: {
        updated: async (uri: string) => void sent.push(uri),
        listChanged: async () => void sent.push('list_changed'),
      },
    }
  }

  it('notifies only subscribed URIs', () => {
    const { sent, notify } = recorder()
    const subs = new Subscriptions(notify)
    subs.add('scadbuddy://jobs/j1')
    subs.onEvent({ id: '1', kind: 'job.running', job_id: 'j1', slug: 'k' })
    subs.onEvent({ id: '2', kind: 'job.running', job_id: 'j2', slug: 'k' })
    expect(sent).toEqual(['scadbuddy://jobs/j1'])
  })

  it('sends list_changed whether or not anything is subscribed', () => {
    const { sent, notify } = recorder()
    new Subscriptions(notify).onEvent({ id: '1', kind: 'model.created', slug: 'k' })
    expect(sent).toEqual(['list_changed'])
  })

  it('coalesces a burst on one URI into the first and one trailing notification', () => {
    vi.useFakeTimers()
    const { sent, notify } = recorder()
    const subs = new Subscriptions(notify, { minIntervalMs: 250 })
    subs.add('scadbuddy://jobs/j1')
    for (let i = 0; i < 50; i++) subs.onEvent({ id: `${i}`, kind: 'job.running', job_id: 'j1', slug: 'k' })
    expect(sent).toEqual(['scadbuddy://jobs/j1'])
    vi.advanceTimersByTime(250)
    expect(sent).toEqual(['scadbuddy://jobs/j1', 'scadbuddy://jobs/j1'])
    vi.advanceTimersByTime(1000)
    expect(sent).toHaveLength(2)
    // After a quiet window, the next event goes out at once again.
    subs.onEvent({ id: 'late', kind: 'job.done', job_id: 'j1', slug: 'k' })
    expect(sent).toHaveLength(3)
  })

  it('keeps coalescing when the wall clock steps forward (#1485)', () => {
    vi.useFakeTimers()
    const { sent, notify } = recorder()
    const subs = new Subscriptions(notify, { minIntervalMs: 250 })
    subs.add('scadbuddy://jobs/j1')
    subs.onEvent({ id: '1', kind: 'job.running', job_id: 'j1', slug: 'k' })
    vi.setSystemTime(Date.now() + 3_600_000)
    subs.onEvent({ id: '2', kind: 'job.running', job_id: 'j1', slug: 'k' })
    expect(sent).toEqual(['scadbuddy://jobs/j1'])
    vi.advanceTimersByTime(250)
    expect(sent).toHaveLength(2)
  })

  it('re-announces session resources after the bus reconnects, and nothing else (#300)', () => {
    const { sent, notify } = recorder()
    const source = new MemoryEventSource()
    const hub = new ResourceHub(source)
    const { subscriptions } = hub.attach(notify)
    subscriptions.add('scadbuddy://jobs/j1')
    subscriptions.add('scadbuddy://sessions/s1')
    subscriptions.add('scadbuddy://sessions')
    source.reconnected()
    expect(sent.sort()).toEqual(['scadbuddy://sessions', 'scadbuddy://sessions/s1'])
    hub.close()
  })

  it('coalesces per URI, not across URIs', () => {
    vi.useFakeTimers()
    const { sent, notify } = recorder()
    const subs = new Subscriptions(notify, { minIntervalMs: 250 })
    subs.add('scadbuddy://jobs/a')
    subs.add('scadbuddy://jobs/b')
    subs.onEvent({ id: '1', kind: 'job.running', job_id: 'a', slug: 'k' })
    subs.onEvent({ id: '2', kind: 'job.running', job_id: 'b', slug: 'k' })
    expect(sent).toEqual(['scadbuddy://jobs/a', 'scadbuddy://jobs/b'])
  })

  it('drops a pending trailing notification when the URI is unsubscribed', () => {
    vi.useFakeTimers()
    const { sent, notify } = recorder()
    const subs = new Subscriptions(notify, { minIntervalMs: 250 })
    subs.add('scadbuddy://jobs/a')
    subs.onEvent({ id: '1', kind: 'job.running', job_id: 'a', slug: 'k' })
    subs.onEvent({ id: '2', kind: 'job.running', job_id: 'a', slug: 'k' })
    subs.delete('scadbuddy://jobs/a')
    vi.advanceTimersByTime(1000)
    expect(sent).toEqual(['scadbuddy://jobs/a'])
  })

  it('on resync, notifies every subscription and the list', () => {
    const { sent, notify } = recorder()
    const subs = new Subscriptions(notify)
    subs.add('scadbuddy://models')
    subs.add('scadbuddy://settings')
    subs.onResync()
    expect(sent.sort()).toEqual(['list_changed', 'scadbuddy://models', 'scadbuddy://settings'])
  })

  it('caps subscriptions per session', () => {
    const subs = new Subscriptions(recorder().notify, { maxSubscriptions: 2 })
    subs.add('scadbuddy://models')
    subs.add('scadbuddy://fonts')
    subs.add('scadbuddy://fonts') // idempotent
    expect(() => subs.add('scadbuddy://libraries')).toThrow(SubscriptionLimitError)
  })

  it('the hub fans one source out to every attached session, and stops on detach', () => {
    const source = new MemoryEventSource()
    const hub = new ResourceHub(source, { minIntervalMs: 0 })
    const a = recorder()
    const b = recorder()
    const sa = hub.attach(a.notify)
    const sb = hub.attach(b.notify)
    sa.subscriptions.add('scadbuddy://fonts')
    sb.subscriptions.add('scadbuddy://fonts')
    source.emit({ id: '1', kind: 'font.installed', family: 'X' })
    expect(a.sent).toEqual(['scadbuddy://fonts'])
    expect(b.sent).toEqual(['scadbuddy://fonts'])
    sb.detach()
    expect(hub.sessions).toBe(1)
    source.emit({ id: '2', kind: 'font.installed', family: 'Y' })
    expect(a.sent).toHaveLength(2)
    expect(b.sent).toHaveLength(1)
    hub.close()
    source.emit({ id: '3', kind: 'font.installed', family: 'Z' })
    expect(a.sent).toHaveLength(2)
  })
})

describe('tool results as resource contents', () => {
  const source = RESOURCES.find((d) => d.name === 'model-source')!
  const thumb = RESOURCES.find((d) => d.name === 'model-thumbnail')!
  const tmf = RESOURCES.find((d) => d.name === 'output-3mf')!

  it('text takes the resource MIME type', () => {
    expect(toContents('scadbuddy://models/k/source', source, { content: [{ type: 'text', text: 'cube(1);' }] })).toEqual([
      { uri: 'scadbuddy://models/k/source', mimeType: 'text/x-openscad', text: 'cube(1);' },
    ])
  })

  it('an image becomes a blob', () => {
    expect(
      toContents('scadbuddy://models/k/thumbnail', thumb, { content: [{ type: 'image', data: 'iVBO', mimeType: 'image/png' }] }),
    ).toEqual([{ uri: 'scadbuddy://models/k/thumbnail', mimeType: 'image/png', blob: 'iVBO' }])
  })

  it('an embedded blob keeps its bytes under the resource URI', () => {
    expect(
      toContents('scadbuddy://outputs/o/model.3mf', tmf, {
        content: [{ type: 'resource', resource: { uri: 'x', mimeType: 'model/3mf', blob: 'UEsD' } }],
      }),
    ).toEqual([{ uri: 'scadbuddy://outputs/o/model.3mf', mimeType: 'model/3mf', blob: 'UEsD' }])
  })

  it('over the inline cap, the content is the JSON note saying where to fetch it', () => {
    const [content] = toContents('scadbuddy://outputs/o/model.3mf', tmf, {
      content: [
        { type: 'resource_link', uri: '/api/v1/outputs/o/model.3mf', name: 'o.3mf' },
        { type: 'text', text: '{"inline":false}' },
      ],
    })
    expect(content).toEqual({ uri: 'scadbuddy://outputs/o/model.3mf', mimeType: 'application/json', text: '{"inline":false}' })
  })

  it('an error result is an internal error', () => {
    expect(() =>
      toContents('scadbuddy://models/k/source', source, { isError: true, content: [{ type: 'text', text: 'boom' }] }),
    ).toThrow(/boom/)
  })
})

describe('the resource list covers the catalogue', () => {
  it('has the fixed resources the issue names', () => {
    const fixed = RESOURCES.filter((d) => !isTemplate(d)).map((d) => d.template)
    expect(fixed.sort()).toEqual(
      [
        'scadbuddy://docs/authoring',
        'scadbuddy://fonts',
        'scadbuddy://libraries',
        'scadbuddy://models',
        'scadbuddy://plates',
        'scadbuddy://sessions',
        'scadbuddy://settings',
      ].sort(),
    )
  })
})
