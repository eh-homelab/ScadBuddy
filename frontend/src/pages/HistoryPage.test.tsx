import { act, screen, waitFor, within } from '@testing-library/react'
import { delay, HttpResponse, http } from 'msw'
import { Route, Routes, useLocation, useParams } from 'react-router'
import { describe, expect, it } from 'vitest'
import { bbox, outputs } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { setDisplayUnit } from '../lib/units'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'
import { EditPage } from './EditPage'
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

  it('shows each output’s size in the display unit', async () => {
    render()
    const nova = await row('Nova')
    expect(within(nova).getByText(/^[\d.]+ × [\d.]+ × [\d.]+ mm$/)).toBeInTheDocument()
    act(() => setDisplayUnit('in'))
    expect(within(nova).getByText(/^\d+\.\d\d × \d+\.\d\d × \d+\.\d\d in$/)).toBeInTheDocument()
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

  it('deep-links each recorded id into Bambuddy', async () => {
    render()
    // The base comes from Settings, because an output record carries ids and no URL.
    const queued = within(await row('Reagan')).getByRole('link', { name: 'queued #4471' })
    expect(queued).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/queue/4471',
    )
    expect(within(await row('Nova')).getByRole('link', { name: 'in library #8790' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/library',
    )
  })

  it('deep-links every plate of a multi-plate print', async () => {
    server.use(
      http.get('/api/v1/models/:slug/outputs', () =>
        HttpResponse.json([
          {
            id: '9'.repeat(32),
            slug: 'name-keychain',
            name: 'Plates',
            job_id: '8'.repeat(32),
            created_at: '2026-09-22T10:00:00Z',
            has_thumbnail: false,
            params: {},
            bbox_mm: bbox(10, 10, 10),
            colors: ['#1B6CA8'],
            queue_item_id: 72,
            slice_job_id: 10,
            plates: [
              { plate_id: 1, queue_item_id: 71, slice_job_id: 9 },
              { plate_id: 2, queue_item_id: 72, slice_job_id: 10 },
            ],
          },
        ]),
      ),
    )
    render()
    const plates = await row('Plates')
    expect(within(plates).getByRole('link', { name: 'plate 1 queued #71' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/queue/71',
    )
    expect(within(plates).getByRole('link', { name: 'plate 2 queued #72' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/queue/72',
    )
    expect(within(plates).queryByText('queued #72')).not.toBeInTheDocument()
  })

  it('edits an output through its deep link', async () => {
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Edit / }))
    expect(await screen.findByTestId('edit-route')).toHaveTextContent('c'.repeat(32))
  })

  it('hands the row it already rendered over rather than making it be resolved again', async () => {
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Edit / }))
    expect(await screen.findByTestId('edit-route')).toHaveTextContent(':Nova')
  })

  it('deletes an output', async () => {
    const { user } = render()
    await user.click(within(await row('Workshop')).getByRole('button', { name: /^Delete / }))

    await waitFor(() => expect(screen.queryAllByText('Workshop')).toHaveLength(0))
    expect(screen.getByTestId('outputs').children).toHaveLength(2)
  })

  it('asks before deleting an output with copies in Bambuddy, and keeps project copies', async () => {
    let asked: string | null = null
    server.use(
      http.delete('/api/v1/outputs/:id', ({ request }) => {
        asked = new URL(request.url).search
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Delete / }))

    const dialog = await screen.findByRole('dialog', { name: 'Delete Nova?' })
    const copies = within(dialog).getByRole('list', { name: 'Library copies' })
    expect(copies).toHaveTextContent('#8790 in the inbox folder')
    expect(copies).toHaveTextContent('#8789 in a project folder, kept')
    await user.click(
      within(dialog).getByRole('checkbox', { name: 'Also delete the inbox copies in Bambuddy' }),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Delete output' }))

    await waitFor(() => expect(screen.queryAllByText('Nova')).toHaveLength(0))
    expect(asked).toBe('?delete_inbox_copies=true')
  })

  it('does not label a copy inbox or project before the settings have loaded', async () => {
    server.use(http.get('/api/v1/settings', () => delay('infinite')))
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Delete / }))

    const dialog = await screen.findByRole('dialog', { name: 'Delete Nova?' })
    const copies = within(dialog).getByRole('list', { name: 'Library copies' })
    expect(copies).not.toHaveTextContent('in the inbox folder')
    expect(copies).not.toHaveTextContent('in a project folder')
    expect(within(copies).getAllByText(/folder not recorded/)).toHaveLength(2)
  })

  it('leaves Bambuddy alone unless the inbox copies are ticked', async () => {
    let asked: string | null = null
    server.use(
      http.delete('/api/v1/outputs/:id', ({ request }) => {
        asked = new URL(request.url).search
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Delete / }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete Nova?' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete output' }))

    await waitFor(() => expect(screen.queryAllByText('Nova')).toHaveLength(0))
    expect(asked).toBe('')
  })

  it('keeps the output and says why when Bambuddy refuses the delete', async () => {
    server.use(
      http.delete('/api/v1/outputs/:id', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Bad Gateway', status: 502, detail: 'Bambuddy said no' },
          { status: 502, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Delete / }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete Nova?' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete output' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Bambuddy said no')
    expect(screen.getByTestId('outputs').children).toHaveLength(3)
  })

  it('invites a first render when there is no history', async () => {
    server.use(http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json([])))
    render()
    expect(await screen.findByRole('heading', { name: 'Nothing generated yet' })).toBeInTheDocument()
  })

  it('offers to send an output again', async () => {
    const { user } = render()
    await user.click(within(await row('Workshop')).getByRole('button', { name: /^Send again / }))

    // #312: the send bar only uploads. There is no mode to choose any more.
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(within(dialog).queryByRole('radio')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Send' })).toBeInTheDocument()
  })
})

describe('template inputs (spec 2026-09-27 §4.3)', () => {
  it('Edit on a row keeps its UI state, and Generate saves it again', async () => {
    const withUi = outputs.map((output) =>
      output.name === 'Nova'
        ? { ...output, inputs: { params: output.params, tab: 'lid', v: 0 } }
        : output,
    )
    const bodies: unknown[] = []
    server.use(
      http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json(withUi)),
      // The row is handed over; a fetch of the edit target would hide a dropped field.
      http.get('/api/v1/outputs/:id/edit', () => new HttpResponse(null, { status: 500 })),
      http.post('/api/v1/models/:slug/outputs', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ ...withUi[0], id: 'd'.repeat(32) }, { status: 201 })
      }),
    )
    const { user } = renderPage(
      <Routes>
        <Route path="/m/:slug/history" element={<HistoryPage />} />
        <Route path="/edit/:outputId" element={<EditPage />} />
        <Route path="/m/:slug" element={<CustomizePage />} />
      </Routes>,
      { route: '/m/name-keychain/history' },
    )
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Edit / }))
    const generate = await screen.findByTestId('generate')
    await waitFor(() => expect(generate).toBeEnabled(), { timeout: 5000 })
    await user.click(generate)
    await waitFor(() => expect(bodies[0]).toMatchObject({ inputs: { tab: 'lid', v: 0 } }))
  }, 15000)
})

describe('HistoryPage, item context (#975)', () => {
  it("names each row's actions after its output", async () => {
    render()
    const nova = await row('Nova')
    expect(within(nova).getByRole('button', { name: 'Edit Nova' })).toBeInTheDocument()
    expect(within(nova).getByRole('button', { name: 'Send again Nova' })).toBeInTheDocument()
    expect(within(nova).getByRole('button', { name: 'Delete Nova' })).toBeInTheDocument()
  })

  it('reads a changed value as the value and its default, not the two run together', async () => {
    render()
    const nova = await row('Nova')
    const values = [...nova.querySelectorAll('dd')].map((dd) => dd.textContent)
    expect(values.length).toBeGreaterThan(0)
    for (const value of values) expect(value).toMatch(/^.+, default .+$/)
    // The old value is marked up as a deletion, not only struck through by CSS.
    expect(nova.querySelector('dd del')).not.toBeNull()
  })
})
