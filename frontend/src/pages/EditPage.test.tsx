import { screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useParams, useSearchParams } from 'react-router'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { EditPage } from './EditPage'

/** Stands in for the customizer so the test can read where the deep link landed. */
function Landing() {
  const { slug } = useParams()
  const [search] = useSearchParams()
  return <div data-testid="landing">{`${slug ?? ''}:${search.get('from') ?? ''}`}</div>
}

function render(outputId: string) {
  return renderPage(
    <Routes>
      <Route path="/edit/:outputId" element={<EditPage />} />
      <Route path="/m/:slug" element={<Landing />} />
    </Routes>,
    { route: `/edit/${outputId}` },
  )
}

describe('EditPage', () => {
  it('lands on the output model customizer with the output to reopen', async () => {
    const id = 'c'.repeat(32)
    render(id)
    expect(await screen.findByTestId('landing')).toHaveTextContent(`name-keychain:${id}`)
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
