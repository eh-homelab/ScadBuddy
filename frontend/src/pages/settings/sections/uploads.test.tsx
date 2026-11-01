import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { server } from '../../../mocks/server'
import { choose } from '../../../test/select'
import { region, renderSettingsSection } from '../testing'
import { Component as Uploads } from './uploads'

const route = '/settings/library'

describe('Uploads', () => {
  it('shows how much the upload store holds against its caps (#296)', async () => {
    server.use(
      http.get('/api/v1/assets/usage', () =>
        HttpResponse.json({
          count: 12,
          bytes: 3_450_000,
          max_count: 10_000,
          max_total_bytes: 1_000_000_000,
        }),
      ),
    )
    renderSettingsSection(<Uploads />, { route })
    const usage = await screen.findByTestId('asset-usage')
    expect(usage).toHaveTextContent('12 of 10000')
    expect(usage).toHaveTextContent('3.5 MB of 1.0 GB')
  })

  it('says a cap of zero is no limit', async () => {
    server.use(
      http.get('/api/v1/assets/usage', () =>
        HttpResponse.json({ count: 2, bytes: 640, max_count: 0, max_total_bytes: 0 }),
      ),
    )
    renderSettingsSection(<Uploads />, { route })
    const usage = await screen.findByTestId('asset-usage')
    expect(usage).toHaveTextContent('2 (no limit)')
    expect(usage).toHaveTextContent('640 B (no limit)')
  })

  it('edits the upload limit in MB or GB and stores bytes', async () => {
    // The default is 1 GiB, shown as exactly that.
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Uploads />, { route })
    const limit = await screen.findByLabelText('Largest media upload')
    expect(limit).toHaveValue(1)
    expect(screen.getByLabelText('Unit for setting-media_upload_max_bytes')).toHaveTextContent('GiB')

    // Picking a unit converts the number; typing one reinterprets it in that unit.
    await choose(user, screen.getByLabelText('Unit for setting-media_upload_max_bytes'), 'MB')
    expect(limit).toHaveValue(1073.742)
    await user.clear(limit)
    await user.type(limit, '500')
    await user.click(within(region('Uploads')).getByRole('button', { name: 'Save Uploads' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ media_upload_max_bytes: 500_000_000 })
    put.mockRestore()
  })

  it('edits the asset fetch allowlist, a domain a line, "none" for none and empty for the defaults (#959)', async () => {
    const defaults = 'game-icons.net\ngithub.com\nraw.githubusercontent.com\nopenmoji.org'
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Uploads />, { route })
    const domains = await screen.findByLabelText('Fetch assets from')
    expect(domains).toHaveValue(defaults)
    const save = () => within(region('Uploads')).getByRole('button', { name: 'Save Uploads' })
    // The next edit starts once the save has landed and its fields are re-seeded.
    const landed = async (call: number) => {
      await put.mock.results[call]?.value
      await waitFor(() => expect(within(region('Uploads')).getByText(/Saved at/)).toBeInTheDocument())
    }

    // Pasted: each keystroke re-renders all of Settings (#1485).
    await user.clear(domains)
    await user.paste('example.com, icons.example.org\n\nexample.com')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    expect(put.mock.calls[0]?.[0]).toEqual({ asset_fetch_domains: ['example.com', 'icons.example.org'] })
    await waitFor(() => expect(domains).toHaveValue('example.com\nicons.example.org'))
    await landed(0)

    await user.clear(domains)
    await user.paste('none')
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toEqual({ asset_fetch_domains: [] })
    await waitFor(async () => expect((await api.getSettings()).asset_fetch_domains).toEqual([]))
    await waitFor(() => expect(domains).toHaveValue('none'))
    await landed(1)

    await user.clear(domains)
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalledTimes(3))
    expect(put.mock.calls[2]?.[0]).toEqual({ asset_fetch_domains: null })
    await waitFor(() => expect(domains).toHaveValue(defaults))
    put.mockRestore()
  })
})
