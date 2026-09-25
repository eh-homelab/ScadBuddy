import { screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useLocation, useParams, useSearchParams } from 'react-router'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { EditPage } from './EditPage'

/** Stands in for the customizer so the test can read where the deep link landed. */
function Landing() {
  const { slug } = useParams()
  const [search] = useSearchParams()
  const state = useLocation().state as { editTarget?: { name?: string | null } } | null
  return (
    <div data-testid="landing">
      {`${slug ?? ''}:${search.get('from') ?? ''}:${state?.editTarget?.name ?? 'no-state'}`}
    </div>
  )
}

function render(outputId: string, state?: unknown) {
  return renderPage(
    <Routes>
      <Route path="/edit/:outputId" element={<EditPage />} />
      <Route path="/m/:slug" element={<Landing />} />
    </Routes>,
    { route: `/edit/${outputId}`, state },
  )
}

describe('EditPage', () => {
  it('lands on the output model customizer with the output to reopen', async () => {
    const id = 'c'.repeat(32)
    render(id)
    expect(await screen.findByTestId('landing')).toHaveTextContent(`name-keychain:${id}`)
  })

  it('hands the resolved target to the customizer instead of making it refetch', async () => {
    render('c'.repeat(32))
    // The name proves the payload travelled, not just the id in the query string.
    expect(await screen.findByTestId('landing')).toHaveTextContent(':Nova')
  })

  it('uses a target the caller already had instead of resolving it again', async () => {
    const id = 'c'.repeat(32)
    // The route 404s, so landing at all proves the handed-over target was used.
    server.use(
      http.get('/api/v1/outputs/:id/edit', () =>
        HttpResponse.json({ title: 'Not found', status: 404 }, { status: 404 }),
      ),
    )
    const editTarget = {
      output_id: id,
      slug: 'name-keychain',
      name: 'Handed over',
      params: {},
      model_version: null,
      source: 'record',
    }
    render(id, { editTarget })
    expect(await screen.findByTestId('landing')).toHaveTextContent(
      `name-keychain:${id}:Handed over`,
    )
  })

  it('does not flash the spinner when the target was handed over', () => {
    const id = 'c'.repeat(32)
    const editTarget = {
      output_id: id,
      slug: 'name-keychain',
      name: 'Handed over',
      params: {},
      model_version: null,
      source: 'record',
    }
    // Synchronous on purpose: nothing has to resolve, so the first paint is the
    // landing, not "Opening that output".
    render(id, { editTarget })
    expect(screen.getByTestId('landing')).toBeInTheDocument()
  })

  it('says so when neither the record nor a 3MF is left', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/edit', () =>
        HttpResponse.json({ title: 'Not found', status: 404 }, { status: 404 }),
      ),
    )
    render('0'.repeat(32))
    expect(await screen.findByRole('alert')).toHaveTextContent('That output is gone')
    expect(screen.queryByTestId('landing')).not.toBeInTheDocument()
  })
})
