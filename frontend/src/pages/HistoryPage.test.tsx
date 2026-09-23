import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { HistoryPage } from './HistoryPage'

function render() {
  return renderPage(<HistoryPage />, { route: '/m/name-keychain/history', path: '/m/:slug/history' })
}

describe('HistoryPage', () => {
  it('lists every output newest first', async () => {
    render()
    const list = await screen.findByTestId('outputs')
    expect(list.children).toHaveLength(3)
    expect(within(list.children[0] as HTMLElement).getByText('out-20260921-1931')).toBeInTheDocument()
  })

  it('diffs each output against the model defaults', async () => {
    render()
    const row = (await screen.findByText('out-20260920-1122')).closest('li') as HTMLElement

    const textSize = within(row).getByText('Text size').parentElement as HTMLElement
    expect(textSize).toHaveTextContent('18')
    expect(textSize).toHaveTextContent('14')
    expect(within(row).getByText('Nova')).toBeInTheDocument()
  })

  it('says so when an output used the defaults', async () => {
    server.use(
      http.get('/api/v1/models/:slug/outputs', () =>
        HttpResponse.json([
          {
            id: 'out-defaults',
            slug: 'name-keychain',
            created_at: '2026-09-22T10:00:00Z',
            params: {},
            colors: ['#1B6CA8'],
          },
        ]),
      ),
    )
    render()
    expect(await screen.findByText('Model defaults, unchanged.')).toBeInTheDocument()
  })

  it('shows where an output already went', async () => {
    render()
    const row = (await screen.findByText('out-20260921-1931')).closest('li') as HTMLElement
    expect(within(row).getByText('queued q-4471')).toBeInTheDocument()

    const library = (await screen.findByText('out-20260920-1122')).closest('li') as HTMLElement
    expect(within(library).getByText('in library lib-8790')).toBeInTheDocument()
  })

  it('re-opens the customizer with that output loaded', async () => {
    render()
    const row = (await screen.findByText('out-20260920-1122')).closest('li') as HTMLElement
    expect(within(row).getByRole('button', { name: 'Re-open' })).toBeEnabled()
  })

  it('deletes an output', async () => {
    const { user } = render()
    const row = (await screen.findByText('out-20260918-0903')).closest('li') as HTMLElement
    await user.click(within(row).getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(screen.queryByText('out-20260918-0903')).not.toBeInTheDocument())
    expect(screen.getByTestId('outputs').children).toHaveLength(2)
  })

  it('invites a first render when there is no history', async () => {
    server.use(http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json([])))
    render()
    expect(await screen.findByRole('heading', { name: 'Nothing generated yet' })).toBeInTheDocument()
  })

  it('offers to send an output again', async () => {
    const { user } = render()
    const row = (await screen.findByText('out-20260918-0903')).closest('li') as HTMLElement
    await user.click(within(row).getByRole('button', { name: 'Send again' }))

    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(within(dialog).getByRole('radio', { name: /Slice and queue/ })).toBeChecked()
  })
})
