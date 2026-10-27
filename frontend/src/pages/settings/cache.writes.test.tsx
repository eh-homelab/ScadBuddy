import { act, screen, waitFor, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { ApiError, api } from '../../api/client'
import type { Settings } from '../../api/types'
import { emitRealtime } from '../../mocks/realtime'
import { SECTION_REGISTRY, type SettingsSection } from './registry'
import { enter, region, renderSettingsShell } from './testing'

/**
 * #4776, #4803…#4839 — the order of saves, resets and reads, through two real sections
 * (Connection and Rendering) in the shell, which offers "Load the latest". Rendering is
 * moved into General here, so both are in the one area shown.
 */
const SECTIONS: readonly SettingsSection[] = SECTION_REGISTRY.filter(
  (section) => section.id === 'connection' || section.id === 'rendering',
).map((section) => ({ ...section, area: 'general' }))

const renderSaves = () => renderSettingsShell(SECTIONS, { route: '/settings/general' })

/** The form seeds itself from the server, so wait for the URL to arrive. */
async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
  )
  await screen.findByLabelText('Render timeout')
}

/** Every microtask queued so far, and every one they queue: a macrotask runs only after them.
 *  Needs real timers: under `vi.useFakeTimers()` this `setTimeout` never fires (#4831 review). */
const flushMicrotasks = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

describe('Settings saves, resets and reads in order (#4776)', () => {
  // A failed test's spies would wrap the next test's.
  afterEach(() => vi.restoreAllMocks())

  it('keeps a later save’s settings when an earlier save answers last (#4776)', async () => {
    // Rendering's answer is held until Connection's has landed. It holds the settings from
    // before Connection's save, so caching it would take the Bambuddy URL back a step.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      if ('render_timeout' in body) await held
      return saved
    })
    const { user } = renderSaves()
    await seeded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    const url = screen.getByLabelText('Bambuddy URL')
    await user.clear(url)
    await enter(user, url, 'https://later.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(within(region('Connection')).queryByText('Unsaved')).toBeNull())

    await act(async () => release())
    await waitFor(() => expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull())
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here')
    expect(timeout).toHaveValue(45)
    // Connection stays clean: the cached Bambuddy URL is the one its save stored.
    expect(url).toHaveValue('https://later.test')
    expect(within(region('Connection')).queryByText('Unsaved')).toBeNull()
    expect(screen.getByTestId('source-bambuddy_url')).toHaveTextContent('Set here')
    put.mockRestore()
  })

  it('keeps a reset when a save sent before it answers last (#4803)', async () => {
    const { user } = renderSaves()
    await seeded()
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await enter(user, field, 'https://mine.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))

    // Rendering's answer is held until the reset's has landed. It holds the stored URL, so
    // caching it would take the reset back.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      if ('render_timeout' in body) await held
      return saved
    })
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    await user.click(
      within(screen.getByTestId('source-public_url')).getByRole('button', { name: 'Reset to deployment value: ScadBuddy’s own URL' }),
    )
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL'))

    await act(async () => release())
    await waitFor(() => expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull())
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here')
    expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL')
    expect(field).toHaveValue('https://scadbuddy.internal.nullreference.io')
    expect(within(region('Connection')).queryByText('Unsaved')).toBeNull()
    put.mockRestore()
  })

  it('keeps an older save’s fields when the server applied a newer one first (#4803)', async () => {
    // Rendering's save reaches the server only after Connection's has been applied and
    // answered, so Connection's answer lacks Rendering's change, and Rendering's own answer,
    // landing last, is not the newest.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      if ('render_timeout' in body) await held
      return save(body)
    })
    const { user } = renderSaves()
    await seeded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    const url = screen.getByLabelText('Bambuddy URL')
    await user.clear(url)
    await enter(user, url, 'https://later.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(within(region('Connection')).queryByText('Unsaved')).toBeNull())
    expect(screen.getByTestId('source-bambuddy_url')).toHaveTextContent('Set here')

    await act(async () => release())
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull()
    expect(timeout).toHaveValue(45)
    expect(url).toHaveValue('https://later.test')
    expect(within(region('Connection')).queryByText('Unsaved')).toBeNull()
    put.mockRestore()
  })

  it('patches an older save’s fields when a newer write for them failed (#4807)', async () => {
    const { user } = renderSaves()
    await seeded()
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await enter(user, field, 'https://mine.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))

    // Connection's save reaches the server only after Rendering's has been applied and
    // answered; the reset sent between them is refused. Rendering's answer, the newest,
    // lacks Connection's URL, so Connection's late answer must still patch it in.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      if ('reset' in body) throw new ApiError(409, 'The reset failed.')
      if ('public_url' in body) await held
      return save(body)
    })
    await user.clear(field)
    await enter(user, field, 'https://older.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    await user.click(
      within(screen.getByTestId('source-public_url')).getByRole('button', { name: 'Reset to deployment value: ScadBuddy’s own URL' }),
    )
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('The reset failed.')).toBeInTheDocument()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))

    await act(async () => release())
    await waitFor(() => expect(within(region('Connection')).queryByText('Unsaved')).toBeNull())
    expect(field).toHaveValue('https://older.test')
    expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here')
    expect(timeout).toHaveValue(45)
    put.mockRestore()
  })

  it('keeps a newer write’s answer for a field when a write between them is refused (#4816)', async () => {
    const { user } = renderSaves()
    await seeded()
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await enter(user, field, 'https://mine.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))

    // The server applies each Connection save as it comes (older, then newest) and holds
    // its answer; the reset sent between them is refused once newest is in flight. The
    // newest's answer lands first, the older's last: the refusal must not hand the URL
    // back to the older save, whose late answer would then patch it over the newest.
    const save = api.putSettings.bind(api)
    const hold = () => {
      let release: () => void = () => undefined
      const held = new Promise<void>((resolve) => (release = resolve))
      return { held, release: () => release() }
    }
    const older = hold()
    const refusal = hold()
    const newest = hold()
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      if ('reset' in body) {
        await refusal.held
        throw new ApiError(409, 'The reset was refused.')
      }
      const saved = await save(body)
      if (body.public_url === 'https://older.test') await older.held
      if (body.public_url === 'https://newest.test') await newest.held
      return saved
    })
    await user.clear(field)
    await enter(user, field, 'https://older.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    await user.click(
      within(screen.getByTestId('source-public_url')).getByRole('button', { name: 'Reset to deployment value: ScadBuddy’s own URL' }),
    )
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))

    // A Rendering save in between.
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))

    await user.clear(field)
    await enter(user, field, 'https://newest.test')
    // Save Connection waits for the section's save in flight; Test connection saves first
    // and does not, so the newest save is sent beside the older one.
    await user.click(within(region('Connection')).getByRole('button', { name: 'Test connection' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(4))

    await act(async () => refusal.release())
    expect(await screen.findByText('The reset was refused.')).toBeInTheDocument()
    await act(async () => newest.release())
    await waitFor(() => expect(within(region('Connection')).queryByText('Unsaved')).toBeNull())
    await act(async () => older.release())
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(field).toHaveValue('https://newest.test')
    expect(within(region('Connection')).queryByText('Unsaved')).toBeNull()
    expect(timeout).toHaveValue(45)
    put.mockRestore()
  })

  it('reads the settings again after a write whose answer was lost (#4816)', async () => {
    const { user } = renderSaves()
    await seeded()
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await enter(user, field, 'https://mine.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))

    // The server applies Connection's save, then the reset; Connection's answer is held,
    // and the reset's is lost to a proxy 502. The reset may well have been applied, so
    // Connection's late answer must not put its URL back over what the server holds.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      if ('reset' in body) throw new ApiError(502, 'Bad gateway.')
      if ('public_url' in body) await held
      return saved
    })
    await user.clear(field)
    await enter(user, field, 'https://older.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    await user.click(
      within(screen.getByTestId('source-public_url')).getByRole('button', { name: 'Reset to deployment value: ScadBuddy’s own URL' }),
    )
    expect(await screen.findByText('Bad gateway.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL'))

    await act(async () => release())
    await waitFor(() => expect(within(region('Connection')).queryByText('Unsaved')).toBeNull())
    expect(field).toHaveValue('https://scadbuddy.internal.nullreference.io')
    expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL')
    put.mockRestore()
  })

  it('says it could not confirm the settings when the read after a lost answer fails too (#4818 review)', async () => {
    const { user } = renderSaves()
    await seeded()
    // The reset is applied but its answer is lost to a proxy 502, and the read that would
    // confirm what the server holds fails as well (still offline): nothing reads again.
    const save = api.putSettings.bind(api)
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      await save(body)
      throw new ApiError(502, 'Bad gateway.')
    })
    const get = vi.spyOn(api, 'getSettings').mockRejectedValue(new TypeError('Failed to fetch'))
    await user.click(
      within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }),
    )
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(await screen.findByText(/Bad gateway\. Could not confirm what the server stored\./)).toBeInTheDocument()
    get.mockRestore()
    put.mockRestore()
  })

  it('says once that it could not confirm two lost writes whose rereads merge (#4821 review)', async () => {
    const { user } = renderSaves()
    await seeded()
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await enter(user, field, 'https://mine.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))
    // Two resets, both applied and both answers lost in the same tick: their rereads
    // merge into one read, which fails and calls each reset's `failed`.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    let applied = 0
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      await save(body)
      applied += 1
      await held
      throw new ApiError(502, 'Bad gateway.')
    })
    const get = vi.spyOn(api, 'getSettings').mockRejectedValue(new TypeError('Failed to fetch'))
    await user.click(
      within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }),
    )
    await user.click(
      within(screen.getByTestId('source-public_url')).getByRole('button', { name: 'Reset to deployment value: ScadBuddy’s own URL' }),
    )
    await waitFor(() => expect(applied).toBe(2))
    await act(async () => release())
    const shown = await screen.findByText(/Could not confirm what the server stored\./)
    // The one merged reread has failed; let every `failed` it calls run.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
    await act(flushMicrotasks)
    expect(shown.textContent?.match(/Could not confirm/g)).toHaveLength(1)
    get.mockRestore()
    put.mockRestore()
  })

  it('says a lost save could not be confirmed when a newer read overtakes its reread and both fail (#4837)', async () => {
    const { user } = renderSaves()
    await seeded()
    const timeout = screen.getByLabelText('Render timeout')
    const save = api.putSettings.bind(api)
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      await save(body)
      throw new ApiError(502, 'Bad gateway.')
    })
    // The save's reread is held; the read "Load the latest" starts after it fails at once.
    let failReread: () => void = () => undefined
    const reread = new Promise<never>((_, reject) => (failReread = () => reject(new TypeError('Failed to fetch'))))
    const get = vi
      .spyOn(api, 'getSettings')
      .mockImplementationOnce(() => reread)
      .mockRejectedValue(new TypeError('Failed to fetch'))
    const note = /Could not confirm what the server stored\./

    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    expect(await within(region('Rendering')).findByText('Bad gateway.')).toBeInTheDocument()
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1))

    // A realtime signal while the form is edited offers the latest; taking it reads again.
    emitRealtime('settings.changed', ['settings'], { section: 'rendering' })
    await user.click(await screen.findByRole('button', { name: 'Load the latest' }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    await act(async () => failReread())
    await act(flushMicrotasks)

    const shown = await within(region('Rendering')).findByText(/^Bad gateway\. Could not confirm what the server stored\./)
    expect(shown.textContent?.match(new RegExp(note, 'g'))).toHaveLength(1)
    expect(screen.getAllByText(note)).toHaveLength(1)
    get.mockRestore()
    put.mockRestore()
  })

  /** A `getSettings` answer that waits for `release`, then reads (or fails, with `fail`). */
  function heldRead() {
    const read = api.getSettings.bind(api)
    let release: () => void = () => undefined
    let fail: () => void = () => undefined
    const answer = new Promise<Settings>((resolve, reject) => {
      release = () => resolve(read())
      fail = () => reject(new TypeError('Failed to fetch'))
    })
    return { answer, release, fail }
  }

  /** Every read from here on fails; reads one, through "Load the latest" when it is offered. */
  async function failNextRead(user: UserEvent, get: MockInstance<typeof api.getSettings>) {
    get.mockRejectedValue(new TypeError('Failed to fetch'))
    const calls = get.mock.calls.length
    emitRealtime('settings.changed', ['settings'], { section: 'rendering' })
    await act(flushMicrotasks)
    const latest = screen.queryByRole('button', { name: 'Load the latest' })
    if (latest) await user.click(latest)
    await waitFor(() => expect(get).toHaveBeenCalledTimes(calls + 1))
    await act(flushMicrotasks)
  }

  it.each([
    ['', false],
    [', and Rendering saved again', true],
  ])(
    'forgets a lost save\u2019s note once its reread is refused for a newer write%s (#4837 review)',
    async (_, saveAgain) => {
      const { user } = renderSaves()
      await seeded()
      const timeout = screen.getByLabelText('Render timeout')
      const save = api.putSettings.bind(api)
      let lost = true
      const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
        const saved = await save(body)
        if ('render_timeout' in body && lost) {
          lost = false
          throw new ApiError(502, 'Bad gateway.')
        }
        return saved
      })
      const reread = heldRead()
      const get = vi.spyOn(api, 'getSettings').mockImplementationOnce(() => reread.answer)

      // 1. Rendering's answer is lost: its reread is held.
      await user.clear(timeout)
      await user.type(timeout, '45')
      await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
      expect(await within(region('Rendering')).findByText('Bad gateway.')).toBeInTheDocument()
      // 2. Connection's save lands first, a newer write.
      const url = screen.getByLabelText('Bambuddy URL')
      await user.clear(url)
      await enter(user, url, 'https://later.test')
      await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
      await waitFor(() => expect(within(region('Connection')).queryByText('Unsaved')).toBeNull())
      // 3. The reread lands, and is refused: Connection's answer is newer.
      await act(async () => reread.release())
      await act(flushMicrotasks)
      // 4. Rendering saves, and its answer is cached whole.
      if (saveAgain) {
        await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
        await waitFor(() => expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull())
      }
      // 5. A later read fails: nothing is left unconfirmed.
      await failNextRead(user, get)
      expect(screen.queryByText(/Could not confirm what the server stored\./)).toBeNull()
      get.mockRestore()
      put.mockRestore()
    },
  )

  it('forgets a lost save\u2019s note once a newer save\u2019s answer is cached whole (#4837 review)', async () => {
    const { user } = renderSaves()
    await seeded()
    const timeout = screen.getByLabelText('Render timeout')
    const save = api.putSettings.bind(api)
    let lost = true
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      if (lost) {
        lost = false
        throw new ApiError(502, 'Bad gateway.')
      }
      return saved
    })
    const reread = heldRead()
    const get = vi.spyOn(api, 'getSettings').mockImplementationOnce(() => reread.answer)

    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    expect(await within(region('Rendering')).findByText('Bad gateway.')).toBeInTheDocument()
    // Saved again before the reread answers: that answer is the whole of what is stored.
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull())
    // The reread, still the newest read, fails.
    await act(async () => reread.fail())
    await act(flushMicrotasks)
    expect(screen.queryByText(/Could not confirm what the server stored\./)).toBeNull()
    get.mockRestore()
    put.mockRestore()
  })

  it('forgets a lost reset\u2019s note once a read lands while the form is edited (#4837 review)', async () => {
    const { user } = renderSaves()
    await seeded()
    const save = api.putSettings.bind(api)
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      await save(body)
      throw new ApiError(502, 'Bad gateway.')
    })
    const reread = heldRead()
    const signalRead = heldRead()
    const get = vi
      .spyOn(api, 'getSettings')
      .mockImplementationOnce(() => reread.answer)
      .mockImplementationOnce(() => signalRead.answer)

    await user.click(
      within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }),
    )
    expect(await screen.findByText('Bad gateway.')).toBeInTheDocument()
    // A realtime signal on a clean form reads; the form is edited before it lands, so the
    // read is refused, yet it returned what is stored.
    emitRealtime('settings.changed', ['settings'], { section: 'rendering' })
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await act(async () => signalRead.release())
    await act(flushMicrotasks)
    // The superseded reread fails, dropped, and a later read fails.
    await act(async () => reread.fail())
    await failNextRead(user, get)
    expect(screen.queryByText(/Could not confirm what the server stored\./)).toBeNull()
    expect(screen.getByText('Bad gateway.')).toBeInTheDocument()
    get.mockRestore()
    put.mockRestore()
  })

  it('says on the section, once, that a lost save could not be confirmed, until a read is taken (#4821 review)', async () => {
    const { user } = renderSaves()
    await seeded()
    const timeout = screen.getByLabelText('Render timeout')
    // Every write is applied with its answer lost, and every read fails (still offline).
    const save = api.putSettings.bind(api)
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      await save(body)
      throw new ApiError(502, 'Bad gateway.')
    })
    const get = vi.spyOn(api, 'getSettings').mockRejectedValue(new TypeError('Failed to fetch'))
    const note = /Could not confirm what the server stored\./

    // A lost reset puts the note on the page error, where the reset reports.
    await user.click(
      within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }),
    )
    expect(await screen.findByText(/^Bad gateway\. Could not confirm what the server stored\./)).toBeInTheDocument()
    const calls = get.mock.calls.length

    // A lost Rendering save puts it on Rendering, and adds no second one to the page error.
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(calls + 1))
    expect(await within(region('Rendering')).findByText(/^Bad gateway\. Could not confirm what the server stored\./)).toBeInTheDocument()
    await act(flushMicrotasks)
    expect(get).toHaveBeenCalledTimes(calls + 1)
    expect(screen.getAllByText(note)).toHaveLength(2)
    for (const shown of screen.getAllByText(note)) expect(shown.textContent?.match(new RegExp(note, 'g'))).toHaveLength(1)

    // Back online: a read taken ("Load the latest", the form being edited) confirms what
    // is stored, and the notes go; the failures they were tacked onto stay.
    get.mockRestore()
    put.mockRestore()
    emitRealtime('settings.changed', ['settings'], { section: 'rendering' })
    await user.click(await screen.findByRole('button', { name: 'Load the latest' }))
    await waitFor(() => expect(screen.queryByText(note)).toBeNull())
    expect(within(region('Rendering')).getByText('Bad gateway.')).toBeInTheDocument()
  })
})
