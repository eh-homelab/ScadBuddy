import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderPage } from './test/utils'

// #2356 — whether three.js was evaluated: the mock's factory runs only on its first import.
const three = vi.hoisted(() => ({ loaded: false }))
vi.mock('three', async (actual) => {
  three.loaded = true
  return await actual<typeof import('three')>()
})

describe('App routes (#2356)', () => {
  it('loads no 3D code for a page that draws none, and loads a page’s own chunk on demand', async () => {
    const { App } = await import('./App')
    renderPage(<App />, { route: '/settings' })
    expect(await screen.findByRole('heading', { name: 'Settings', level: 1 })).toBeInTheDocument()
    expect(three.loaded).toBe(false)
  })
})
