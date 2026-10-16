import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'

const region = () => {
  const section = screen.getByText('Administration', { selector: 'h2 button' }).closest('section')
  if (!section) throw new Error('no Administration section')
  return section
}
const save = () => within(region()).getByRole('button', { name: 'Save Administration' })

// The next edit starts once the save has landed and its fields are re-seeded.
async function landed(put: { mock: { results: { value: unknown }[] } }, call: number) {
  await put.mock.results[call]?.value
  await waitFor(() => expect(within(region()).getByText(/Saved at/)).toBeInTheDocument())
}

async function loaded() {
  await waitFor(() => expect(screen.getByLabelText('Notify me about activity from')).toBeInTheDocument())
  await waitFor(() => expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'))
}

describe('SettingsPage activity settings', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows the defaults as placeholders and sends nothing when another field is saved', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await loaded()
    const follow = screen.getByLabelText('Notify me about activity from')
    expect(follow).toHaveValue('')
    expect(follow).toHaveAttribute('placeholder', 'browser')
    expect(screen.getByLabelText('Keep activity for (seconds)')).toHaveAttribute('placeholder', '2592000')
    expect(screen.getByLabelText('Repair activity rows from the last (seconds)')).toHaveAttribute(
      'placeholder',
      '86400',
    )
    await user.type(screen.getByLabelText('Temporal UI URL'), 'https://t.example')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).not.toHaveProperty('activity_follow')
  })

  it('round-trips a list, follows nobody with "none", and returns to the default when emptied', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await loaded()
    const follow = screen.getByLabelText('Notify me about activity from')

    // Pasted: each keystroke re-renders all of Settings (#1485).
    await user.click(follow)
    await user.paste('browser, token:t1')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    expect(put.mock.calls[0]?.[0]).toMatchObject({ activity_follow: ['browser', 'token:t1'] })
    await waitFor(async () => expect((await api.getSettings()).activity_follow).toEqual(['browser', 'token:t1']))
    expect(follow).toHaveValue('browser, token:t1')
    await landed(put, 0)

    await user.clear(follow)
    await user.paste('none')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toMatchObject({ activity_follow: [] })
    await waitFor(async () => expect((await api.getSettings()).activity_follow).toEqual([]))
    expect(follow).toHaveValue('none')
    await landed(put, 1)

    await user.clear(follow)
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(3))
    expect(put.mock.calls[2]?.[0]).toMatchObject({ activity_follow: null })
  })

  it('checks the retention bounds before sending, and empty is the default', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await loaded()
    const ttl = screen.getByLabelText('Keep activity for (seconds)')
    const repair = screen.getByLabelText('Repair activity rows from the last (seconds)')
    // Folded until opened, as a user finds them; a refused value opens the fold itself.
    await user.click(screen.getByRole('button', { name: 'Advanced: activity retention' }))

    await user.type(ttl, '3600')
    await user.type(repair, '5')
    await user.click(save())
    expect(await screen.findByText('Enter 86400 to 315360000 seconds.')).toBeInTheDocument()
    expect(screen.getByText('Enter 600 to 604800 seconds.')).toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()

    await user.clear(ttl)
    await user.type(ttl, '172800')
    await user.clear(repair)
    await user.type(repair, '3600')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    expect(put.mock.calls[0]?.[0]).toMatchObject({
      activity_ttl_seconds: 172800,
      activity_repair_window_seconds: 3600,
    })
    await waitFor(async () => expect((await api.getSettings()).activity_ttl_seconds).toBe(172800))
    await landed(put, 0)

    await user.clear(ttl)
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toMatchObject({ activity_ttl_seconds: null })
  })

  it('keeps the retention fields under Advanced', async () => {
    renderPage(<SettingsPage />)
    await loaded()
    const fold = '[data-slot=collapsible]'
    const details = screen.getByLabelText('Keep activity for (seconds)').closest(fold)
    expect(details).toHaveTextContent('Advanced: activity retention')
    expect(details).not.toBe(screen.getByLabelText('Notify me about activity from').closest(fold))
    expect(details).toContainElement(screen.getByLabelText('Repair activity rows from the last (seconds)'))
  })

  it('opens the fold when a save refuses a field folded inside it (#3671)', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await loaded()
    const toggle = screen.getByRole('button', { name: 'Advanced: activity retention' })
    const ttl = screen.getByLabelText('Keep activity for (seconds)')

    await user.click(toggle)
    await user.type(ttl, '3600')
    await user.click(toggle)
    expect(ttl).not.toBeVisible()

    await user.click(save())
    expect(await screen.findByText('Enter 86400 to 315360000 seconds.')).toBeVisible()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(ttl).toBeVisible()
    expect(put).not.toHaveBeenCalled()
  })
})
