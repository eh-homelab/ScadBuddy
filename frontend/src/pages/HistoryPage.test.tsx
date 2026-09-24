import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useLocation, useParams } from 'react-router'
import { describe, expect, it } from 'vitest'
import { bbox } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { HistoryPage } from './HistoryPage'

function render() {
  return renderPage(
    <Routes>
      <Route path="/m/:slug/history" element={<HistoryPage />} />
      <Route path="/edit/:outputId" element={<EditRoute />} />
    </Routes>,
    { route: '/m/name-keychain/history' },
  )
}

/** Stands in for the deep-link page so the test can read what it was given. */
function EditRoute() {
  const { outputId } = useParams()
  const state = useLocation().state as { editTarget?: { name?: string | null } } | null
  return <div data-testid="edit-route">{`${outputId ?? ''}:${state?.editTarget?.name ?? 'no-state'}`}</div>
}

/**
 * Rows are labelled by the output's name — its id is 32 hex characters. The name is
 * also one of the parameters, so it appears twice in its own row; the heading is first.
 */
async function row(name: string): Promise<HTMLElement> {
  const headings = await screen.findAllByText(name)
  return (headings[0] as HTMLElement).closest('li') as HTMLElement
}

describe('HistoryPage', () => {
  it('lists every output newest first', async () => {
    render()
    const list = await screen.findByTestId('outputs')
    expect(list.children).toHaveLength(3)
    expect(within(list.children[0] as HTMLElement).getByText('Reagan')).toBeInTheDocument()
  })

  it('diffs each output against the model defaults', async () => {
    render()
    const nova = await row('Nova')

    const textSize = within(nova).getByText('Text size').parentElement as HTMLElement
    expect(textSize).toHaveTextContent('18')
    expect(textSize).toHaveTextContent('14')
  })

  it('says so when an output used the defaults', async () => {
    server.use(
      http.get('/api/v1/models/:slug/outputs', () =>
        HttpResponse.json([
          {
            id: '9'.repeat(32),
            slug: 'name-keychain',
            name: 'Defaults',
            job_id: '8'.repeat(32),
            created_at: '2026-09-22T10:00:00Z',
            has_thumbnail: false,
            params: {},
            bbox_mm: bbox(10, 10, 10),
            colors: ['#1B6CA8'],
          },
        ]),
      ),
    )
    render()
    expect(await screen.findByText('Model defaults, unchanged.')).toBeInTheDocument()
  })

  it('shows where an output already went, by Bambuddy id', async () => {
    render()
    expect(within(await row('Reagan')).getByText('queued #4471')).toBeInTheDocument()
    expect(within(await row('Nova')).getByText('in library #8790')).toBeInTheDocument()
  })

  it('edits an output through its deep link', async () => {
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: 'Edit' }))
    expect(await screen.findByTestId('edit-route')).toHaveTextContent('c'.repeat(32))
  })

  it('hands the row it already rendered over rather than making it be resolved again', async () => {
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: 'Edit' }))
    expect(await screen.findByTestId('edit-route')).toHaveTextContent(':Nova')
  })

  it('deletes an output', async () => {
    const { user } = render()
    await user.click(within(await row('Workshop')).getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(screen.queryAllByText('Workshop')).toHaveLength(0))
    expect(screen.getByTestId('outputs').children).toHaveLength(2)
  })

  it('invites a first render when there is no history', async () => {
    server.use(http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json([])))
    render()
    expect(await screen.findByRole('heading', { name: 'Nothing generated yet' })).toBeInTheDocument()
  })

  it('offers to send an output again', async () => {
    const { user } = render()
    await user.click(within(await row('Workshop')).getByRole('button', { name: 'Send again' }))

    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(within(dialog).getByRole('radio', { name: /Slice and queue/ })).toBeChecked()
  })
})
