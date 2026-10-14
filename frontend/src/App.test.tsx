import { screen } from '@testing-library/react'
import type * as Three from 'three'
import { describe, expect, it, vi } from 'vitest'
import { renderPage } from './test/utils'

// #2356 — whether three.js was evaluated: the mock's factory runs only on its first import.
const three = vi.hoisted(() => ({ loaded: false }))
vi.mock('three', async (actual) => {
  three.loaded = true
  return await actual<typeof Three>()
})

describe('App routes (#2356)', () => {
  it('loads no 3D code for a page that draws none, and loads a page’s own chunk on demand', async () => {
    // From a fresh module graph, so no case before this one has already imported three.
    vi.resetModules()
    three.loaded = false
    const { App } = await import('./App')
    renderPage(<App />, { route: '/settings' })
    // While the chunk loads, a screen reader is told so.
    expect(screen.getByRole('status')).toHaveTextContent('Loading')
    expect(await screen.findByRole('heading', { name: 'Settings', level: 1 })).toBeInTheDocument()
    expect(three.loaded).toBe(false)
  })

  // A lazy page is looked up by its export's name, which no type checks: one renamed
  // would leave its route blank.
  it.each([
    ['/prints', 'Print history'],
    ['/m/chunky-name-sign/prints', 'Prints'],
  ])('renders %s through its lazy chunk', async (route, heading) => {
    const { App } = await import('./App')
    renderPage(<App />, { route })
    expect(await screen.findByRole('heading', { name: heading, level: 1 })).toBeInTheDocument()
  })
})
