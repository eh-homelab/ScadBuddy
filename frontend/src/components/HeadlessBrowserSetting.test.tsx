import { act, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { announceHeadlessBrowser } from '../lib/headlessBrowserSwitch'
import { renderPage } from '../test/utils'
import { HeadlessBrowserSetting } from './HeadlessBrowserSetting'

const LABEL = 'Let AI sessions use ScadBuddy in a headless browser'

describe('HeadlessBrowserSetting (#349)', () => {
  it('is off by default, and turns on and off at once', async () => {
    const { user } = renderPage(<HeadlessBrowserSetting />)
    const box = await screen.findByRole('checkbox', { name: LABEL })
    expect(box).not.toBeChecked()

    await user.click(box)
    await waitFor(() => expect(box).toBeChecked())
    expect(await api.getHeadlessBrowserSetting()).toEqual({ enabled: true })

    await user.click(box)
    await waitFor(() => expect(box).not.toBeChecked())
    expect(await api.getHeadlessBrowserSetting()).toEqual({ enabled: false })
  })

  // #2143 (#1904): the plugin card's switch writes the same key; this one follows it.
  it('follows the playwright card when it flips the setting', async () => {
    renderPage(<HeadlessBrowserSetting />)
    const box = await screen.findByRole('checkbox', { name: LABEL })
    expect(box).not.toBeChecked()
    act(() => announceHeadlessBrowser(true))
    expect(box).toBeChecked()
    act(() => announceHeadlessBrowser(false))
    expect(box).not.toBeChecked()
  })

  it('announces what it saved, so the playwright card follows it', async () => {
    const heard = vi.fn()
    const listen = (event: Event) => heard((event as CustomEvent<boolean>).detail)
    window.addEventListener('scadbuddy:headless-browser', listen)
    try {
      const { user } = renderPage(<HeadlessBrowserSetting />)
      const box = await screen.findByRole('checkbox', { name: LABEL })
      await user.click(box)
      await waitFor(() => expect(heard).toHaveBeenLastCalledWith(true))
      await user.click(box)
      await waitFor(() => expect(heard).toHaveBeenLastCalledWith(false))
      expect(heard).toHaveBeenCalledTimes(2)
    } finally {
      window.removeEventListener('scadbuddy:headless-browser', listen)
    }
  })

  it('announces nothing when the save is refused', async () => {
    server.use(
      http.put('/api/v1/ai/settings/headless-browser', () => HttpResponse.json({ detail: 'no' }, { status: 403 })),
    )
    const heard = vi.fn()
    window.addEventListener('scadbuddy:headless-browser', heard)
    try {
      const { user } = renderPage(<HeadlessBrowserSetting />)
      await user.click(await screen.findByRole('checkbox', { name: LABEL }))
      await screen.findByRole('alert')
      expect(heard).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('scadbuddy:headless-browser', heard)
    }
  })

  it('is for the user only: an agent cannot flip it', async () => {
    renderPage(<HeadlessBrowserSetting />)
    const box = await screen.findByRole('checkbox', { name: LABEL })
    expect(box.closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows why a save was refused, and keeps the stored value', async () => {
    server.use(
      http.put('/api/v1/ai/settings/headless-browser', () =>
        HttpResponse.json(
          { detail: 'changes to headless browser settings must come from the ScadBuddy UI' },
          { status: 403 },
        ),
      ),
    )
    const { user } = renderPage(<HeadlessBrowserSetting />)
    const box = await screen.findByRole('checkbox', { name: LABEL })
    await user.click(box)
    expect(await screen.findByRole('alert')).toHaveTextContent('must come from the ScadBuddy UI')
    expect(box).not.toBeChecked()
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/headless-browser', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<HeadlessBrowserSetting />)
    await waitFor(() =>
      expect(screen.queryByRole('checkbox', { name: LABEL })).not.toBeInTheDocument(),
    )
    // Give the failed read time to land; still nothing.
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('AI headless browser')).not.toBeInTheDocument()
  })
})
