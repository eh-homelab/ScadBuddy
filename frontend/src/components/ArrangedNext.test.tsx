import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type * as embed from '../lib/embed'
import { DownloadBlockedError, DownloadWindowClosedError, downloadBlob } from '../lib/embed'
import type { Output } from '../api/types'
import { outputs } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ArrangedNext } from './ArrangedNext'

vi.mock('../lib/embed', async (original) => ({
  ...(await original<typeof embed>()),
  downloadBlob: vi.fn(),
}))

const output = outputs[0] as Output
const top = window.top

describe('ArrangedNext (#2287)', () => {
  beforeEach(() => {
    vi.mocked(downloadBlob).mockReset()
    // Inside Bambuddy's iframe: its sandbox has no allow-downloads (#612).
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
  })
  afterEach(() => {
    Object.defineProperty(window, 'top', { value: top, configurable: true })
  })

  it('downloads the 3MF through downloadBlob when embedded, not a plain link', async () => {
    let loaded: Blob | undefined
    vi.mocked(downloadBlob).mockImplementation(async (load) => {
      loaded = await load()
    })
    const { user } = renderPage(<ArrangedNext output={output} sent={false} onPrint={vi.fn()} />)
    expect(screen.queryByRole('link', { name: 'Download' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() =>
      expect(downloadBlob).toHaveBeenCalledWith(expect.any(Function), `${output.slug}-${output.id}.3mf`),
    )
    await waitFor(() => expect(loaded).toBeInstanceOf(Blob))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says the plates are unavailable when they cannot be read, rather than showing none (#2754 review)', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json(
          { title: 'Bad Gateway', status: 502 },
          { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage(<ArrangedNext output={output} sent={false} onPrint={vi.fn()} />)
    expect(await screen.findByText('Plates unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('figure', { name: 'Plates' })).not.toBeInTheDocument()
  })

  it('shows the plates of the arranged result before its next step (#2282)', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: true },
          { index: 2, has_thumbnail: true },
        ]),
      ),
    )
    renderPage(<ArrangedNext output={output} sent={false} onPrint={vi.fn()} />)
    const plates = await screen.findByRole('figure', { name: 'Plates' })
    expect(plates).toHaveTextContent('2 plates')
    expect(within(plates).getAllByRole('img').map((img) => img.getAttribute('src'))).toEqual([
      `/api/v1/outputs/${output.id}/plates/1/thumbnail`,
      `/api/v1/outputs/${output.id}/plates/2/thumbnail`,
    ])
  })

  it('says why when the popup is blocked or closed', async () => {
    vi.mocked(downloadBlob).mockRejectedValueOnce(new DownloadBlockedError())
    const { user } = renderPage(<ArrangedNext output={output} sent={false} onPrint={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Download' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(new DownloadBlockedError().message)
    vi.mocked(downloadBlob).mockRejectedValueOnce(new DownloadWindowClosedError())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(new DownloadWindowClosedError().message),
    )
  })
})
