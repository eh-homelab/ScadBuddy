import { act, screen, waitFor } from '@testing-library/react'
import { useEffect, useLayoutEffect, useState, type ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { mockSettings, setMockSettings } from '../../mocks/handlers'
import { emitRealtime } from '../../mocks/realtime'
import { renderPage } from '../../test/utils'
import { BambuddyResourcesProvider, useBambuddyResources } from './bambuddyResources'
import { SettingsCacheProvider, SettingsLoaded, useLoadedSettings, useSettingsCache, type Seeder, type SettingsCache } from './cache'
import { SettingsDirtyProvider, useAnyDirty, useDirtySections, usePeerDraft, useReportDirty } from './dirty'
import type { FieldName } from './fields'
import type { Edits } from './seed'

function Providers({ children }: { children: ReactNode }) {
  return (
    <SettingsDirtyProvider>
      <SettingsCacheProvider>
        <BambuddyResourcesProvider>{children}</BambuddyResourcesProvider>
      </SettingsCacheProvider>
    </SettingsDirtyProvider>
  )
}

/** The cache as the last committed render saw it. */
let cache: SettingsCache
function Probe() {
  const seen = useSettingsCache()
  useEffect(() => {
    cache = seen
  })
  return null
}

function Loaded() {
  const { settings } = useSettingsCache()
  return settings ? <span data-testid="loaded" /> : null
}

/** A section's seeder: registered while mounted, its `begin` counted. */
function fakeSeeder(name: string, order: string[]) {
  const since: Edits = new Map<FieldName, number>([['public_url', name.length]])
  const begin = vi.fn(() => {
    order.push(`begin-${name}`)
    return since
  })
  const seed = vi.fn((_names: 'all' | readonly FieldName[], _since: Edits) => {
    order.push(`seed-${name}`)
  })
  return { seeder: { begin, seed } satisfies Seeder, since, begin, seed }
}

function Section({ id, dirty = false, seeder, drafts }: { id: string; dirty?: boolean; seeder?: Seeder; drafts?: Partial<Record<FieldName, string>> }) {
  useReportDirty(id, dirty, drafts)
  const { register } = useSettingsCache()
  useEffect(() => (seeder ? register(seeder) : undefined), [register, seeder])
  return null
}

let dirtyNow: readonly string[] = []
function DirtyProbe() {
  const seen = useDirtySections()
  useEffect(() => {
    dirtyNow = seen
  })
  return null
}

function PeerDraft({ name }: { name: FieldName }) {
  return <span data-testid="peer">{usePeerDraft(name) ?? 'none'}</span>
}

/** Emits a settings event until `check` passes: the first may land before the subscription is live. */
async function emitUntil(check: () => void) {
  await waitFor(() => {
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    check()
  })
}

describe('settings cache', () => {
  it('makes one GET /settings for every section that reads it', async () => {
    const get = vi.spyOn(api, 'getSettings')
    renderPage(
      <Providers>
        <Loaded />
        <Loaded />
        <Loaded />
      </Providers>,
    )
    await waitFor(() => expect(screen.getAllByTestId('loaded')).toHaveLength(3))
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('renders its children only once the settings are loaded', async () => {
    function Url() {
      return <span data-testid="url">{useLoadedSettings().bambuddy_url}</span>
    }
    renderPage(
      <Providers>
        <SettingsLoaded>
          <Url />
        </SettingsLoaded>
      </Providers>,
    )
    expect(screen.queryByTestId('url')).not.toBeInTheDocument()
    expect(await screen.findByTestId('url')).toHaveTextContent(mockSettings().bambuddy_url ?? '')
  })

  it('seeds every seeder with the fields a write names, each since taken before the request', async () => {
    const order: string[] = []
    const a = fakeSeeder('a', order)
    const b = fakeSeeder('bb', order)
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
        <Section id="a" seeder={a.seeder} />
        <Section id="b" seeder={b.seeder} />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    let next: Awaited<ReturnType<typeof api.putSettings>> | undefined
    await act(async () => {
      next = await cache.write(['store_backend'], async () => {
        order.push('request')
        return api.putSettings({ store_backend: 'local' })
      })
    })
    expect(order).toEqual(['begin-a', 'begin-bb', 'request', 'seed-a', 'seed-bb'])
    expect(a.seed).toHaveBeenCalledWith(['store_backend'], a.since)
    expect(b.seed).toHaveBeenCalledWith(['store_backend'], b.since)
    expect(cache.settings).toBe(next)
  })

  it('seeds the fields a write names from its answer', async () => {
    const order: string[] = []
    const a = fakeSeeder('a', order)
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
        <Section id="a" seeder={a.seeder} />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    await act(async () => {
      await cache.write(
        (next) => (next.store_backend === 'local' ? ['store_backend'] : []),
        () => api.putSettings({ store_backend: 'local' }),
      )
    })
    expect(a.seed).toHaveBeenCalledWith(['store_backend'], a.since)
  })

  it("ignores its own write's settings event", async () => {
    const get = vi.spyOn(api, 'getSettings')
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    // The subscription is live once an event from elsewhere reads again.
    await emitUntil(() => expect(get.mock.calls.length).toBeGreaterThan(1))
    await new Promise((resolve) => setTimeout(resolve, 300))
    const before = get.mock.calls.length

    let release: () => void = () => {}
    let owned: Promise<void> | undefined
    act(() => {
      owned = cache.own(() => new Promise<void>((resolve) => (release = resolve)))
    })
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(get).toHaveBeenCalledTimes(before)
    expect(cache.changedElsewhere).toBe(false)
    await act(async () => {
      release()
      await owned
    })
  })

  it('follows an event while nothing is dirty: every seeder seeds all', async () => {
    const order: string[] = []
    const a = fakeSeeder('a', order)
    const b = fakeSeeder('bb', order)
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
        <Section id="a" seeder={a.seeder} />
        <Section id="b" seeder={b.seeder} />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    await api.putSettings({ public_url: 'https://elsewhere.test' })
    await emitUntil(() => {
      expect(a.seed).toHaveBeenCalledWith('all', a.since)
      expect(b.seed).toHaveBeenCalledWith('all', b.since)
    })
    await waitFor(() => expect(cache.settings?.public_url).toBe('https://elsewhere.test'))
    expect(cache.changedElsewhere).toBe(false)
  })

  it("follows an event with each seeder's edit counts taken before the read is sent (#2171)", async () => {
    const get = vi.spyOn(api, 'getSettings')
    // A seeder whose counts are what has been typed so far: one more keystroke while the
    // read is in flight, not yet reported dirty, must keep its draft.
    let typed = 0
    const seed = vi.fn((_names: 'all' | readonly FieldName[], _since: Edits) => {})
    const seeder: Seeder = { begin: () => new Map<FieldName, number>([['public_url', typed]]), seed }
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
        <Section id="a" seeder={seeder} />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    await emitUntil(() => expect(seed).toHaveBeenCalledWith('all', expect.any(Map)))
    await new Promise((resolve) => setTimeout(resolve, 300))
    seed.mockClear()

    let answer: () => void = () => {}
    get.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (answer = resolve))
      return mockSettings()
    })
    const sent = get.mock.calls.length
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await waitFor(() => expect(get.mock.calls.length).toBe(sent + 1))
    typed++
    await act(async () => answer())
    await waitFor(() => expect(seed).toHaveBeenCalledTimes(1))
    expect(seed).toHaveBeenCalledWith('all', new Map([['public_url', 0]]))
  })

  it('says changed elsewhere, and seeds nothing, when a section turns dirty while the read is in flight', async () => {
    const get = vi.spyOn(api, 'getSettings')
    const order: string[] = []
    const a = fakeSeeder('a', order)
    function Dirtying() {
      const [dirty, setDirty] = useState(false)
      makeDirty = () => setDirty(true)
      return <Section id="a" dirty={dirty} seeder={a.seeder} />
    }
    let makeDirty: () => void = () => {}
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
        <Dirtying />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    await emitUntil(() => expect(a.seed).toHaveBeenCalledWith('all', a.since))
    await new Promise((resolve) => setTimeout(resolve, 300))
    a.seed.mockClear()

    let answer: () => void = () => {}
    get.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (answer = resolve))
      return mockSettings()
    })
    const sent = get.mock.calls.length
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await waitFor(() => expect(get.mock.calls.length).toBe(sent + 1))
    act(() => makeDirty())
    await act(async () => answer())
    await waitFor(() => expect(cache.changedElsewhere).toBe(true))
    expect(a.seed).not.toHaveBeenCalled()
  })

  it('says changed elsewhere while any section is dirty, and Load the latest seeds every seeder', async () => {
    const order: string[] = []
    const a = fakeSeeder('a', order)
    const b = fakeSeeder('bb', order)
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
        <Section id="a" dirty seeder={a.seeder} />
        <Section id="b" seeder={b.seeder} />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    await emitUntil(() => expect(cache.changedElsewhere).toBe(true))
    expect(a.seed).not.toHaveBeenCalledWith('all', expect.anything())
    expect(b.seed).not.toHaveBeenCalledWith('all', expect.anything())

    act(() => cache.loadLatest())
    expect(a.seed).toHaveBeenCalledWith('all', expect.any(Map))
    expect(b.seed).toHaveBeenCalledWith('all', expect.any(Map))
    expect(cache.changedElsewhere).toBe(false)
  })

  it('Keep mine clears the notice and seeds nothing', async () => {
    const order: string[] = []
    const a = fakeSeeder('a', order)
    renderPage(
      <Providers>
        <Probe />
        <Loaded />
        <Section id="a" dirty seeder={a.seeder} />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    await emitUntil(() => expect(cache.changedElsewhere).toBe(true))
    act(() => cache.keepMine())
    expect(cache.changedElsewhere).toBe(false)
    expect(a.seed).not.toHaveBeenCalled()
  })
})

describe('dirty registry', () => {
  it('lists the dirty sections and drops one that unmounts', async () => {
    const { rerender } = renderPage(
      <Providers>
        <DirtyProbe />
        <Section id="a" dirty />
        <Section id="b" />
      </Providers>,
    )
    await waitFor(() => expect(dirtyNow).toEqual(['a']))
    rerender(
      <Providers>
        <DirtyProbe />
        <Section id="b" />
      </Providers>,
    )
    await waitFor(() => expect(dirtyNow).toEqual([]))
  })

  it('reports a section dirty as its render commits, before any passive effect (#2171)', async () => {
    let seenInLayout: boolean | undefined
    function Reader() {
      const anyDirty = useAnyDirty()
      // Runs in the same commit, after the reporter above it in the tree.
      useLayoutEffect(() => {
        seenInLayout = anyDirty()
      })
      return null
    }
    renderPage(
      <Providers>
        <Section id="a" dirty />
        <Reader />
      </Providers>,
    )
    await waitFor(() => expect(seenInLayout).toBeDefined())
    expect(seenInLayout).toBe(true)
  })

  it('hands one section’s published draft to another', async () => {
    renderPage(
      <Providers>
        <Section id="connection" dirty drafts={{ bambuddy_url: 'https://new.test' }} />
        <PeerDraft name="bambuddy_url" />
      </Providers>,
    )
    await waitFor(() => expect(screen.getByTestId('peer')).toHaveTextContent('https://new.test'))
  })
})

describe('Bambuddy resources', () => {
  function Targets() {
    const { connected, targets } = useBambuddyResources()
    return <span data-testid="targets">{connected && targets.data ? 'targets' : 'none'}</span>
  }

  it('fetches no targets until a Bambuddy URL is saved', async () => {
    setMockSettings({ ...mockSettings(), bambuddy_url: null })
    const targets = vi.spyOn(api, 'getBambuddyTargets')
    renderPage(
      <Providers>
        <Loaded />
        <Targets />
      </Providers>,
    )
    await screen.findByTestId('loaded')
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(targets).not.toHaveBeenCalled()
  })

  it('fetches the targets once for every reader', async () => {
    const targets = vi.spyOn(api, 'getBambuddyTargets')
    renderPage(
      <Providers>
        <Targets />
        <Targets />
      </Providers>,
    )
    await waitFor(() => expect(screen.getAllByTestId('targets').map((node) => node.textContent)).toEqual(['targets', 'targets']))
    expect(targets).toHaveBeenCalledTimes(1)
  })
})
