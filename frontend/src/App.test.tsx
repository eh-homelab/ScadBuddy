import { screen, waitFor } from '@testing-library/react'
import type * as Three from 'three'
import { describe, expect, it, onTestFinished, vi } from 'vitest'
import { server } from './mocks/server'
import { renderPage } from './test/utils'

// #2356 — whether three.js was evaluated: the mock's factory runs only on its first import.
const three = vi.hoisted(() => ({ loaded: false }))
vi.mock('three', async (actual) => {
  three.loaded = true
  return await actual<typeof Three>()
})

// The shell's ActivityBell reads settings, then the followed activity, on mount. A case
// that ends with either in flight lets it land after the environment is torn down, and
// its setState then throws "window is not defined" (#2983's CI). Counted per case, so
// each can wait until the shell's reads are answered.
function settled() {
  const started: string[] = []
  const answered: string[] = []
  const name = ({ request }: { request: Request }) =>
    `${request.method} ${new URL(request.url).pathname}`
  const onStart = (event: { request: Request }) => started.push(name(event))
  const onAnswer = (event: { request: Request }) => answered.push(name(event))
  server.events.on('request:start', onStart)
  server.events.on('response:mocked', onAnswer)
  onTestFinished(() => {
    server.events.removeListener('request:start', onStart)
    server.events.removeListener('response:mocked', onAnswer)
  })
  return () =>
    waitFor(() => {
      expect(answered).toContain('GET /api/v1/activity')
      expect(answered.length).toBe(started.length)
    })
}

describe('App routes (#2356)', () => {
  it('loads no 3D code for a page that draws none, and loads a page’s own chunk on demand', async () => {
    // From a fresh module graph, so no case before this one has already imported three.
    vi.resetModules()
    three.loaded = false
    const { App } = await import('./App')
    const done = settled()
    renderPage(<App />, { route: '/settings' })
    // While the chunk loads, a screen reader is told so.
    expect(screen.getByRole('status')).toHaveTextContent('Loading')
    expect(await screen.findByRole('heading', { name: 'Settings', level: 1 })).toBeInTheDocument()
    expect(three.loaded).toBe(false)
    await done()
  })

  // A lazy page is looked up by its export's name, which no type checks: one renamed
  // would leave its route blank.
  it.each([
    ['/prints', 'Print history'],
    ['/m/chunky-name-sign/prints', 'Prints'],
  ])('renders %s through its lazy chunk', async (route, heading) => {
    const { App } = await import('./App')
    const done = settled()
    renderPage(<App />, { route })
    expect(await screen.findByRole('heading', { name: heading, level: 1 })).toBeInTheDocument()
    await done()
  })
})
