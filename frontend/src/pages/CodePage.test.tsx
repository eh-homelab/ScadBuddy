import { screen, waitFor } from '@testing-library/react'
import { Route, Routes, useLocation, useNavigationType } from 'react-router'
import { describe, expect, it } from 'vitest'
import { L, spoolId } from '../mocks/features/filament/seed'
import { renderPage } from '../test/utils'
import { CodePage } from './CodePage'

function Where() {
  const where = useLocation()
  return (
    <>
      <p data-testid="where">{where.pathname}</p>
      <p data-testid="query">{where.search}</p>
      <p data-testid="how">{useNavigationType()}</p>
    </>
  )
}

function render(route: string) {
  return renderPage(
    <Routes>
      <Route path="/x/:code" element={<CodePage />} />
      <Route path="*" element={<Where />} />
    </Routes>,
    { route },
  )
}

describe('the code page', () => {
  it('opens a location code at its page', async () => {
    render('/x/L-3')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/locations/${L.DRY_BOX}`))
  })

  it('opens a spool code at its page, with any case and no dash', async () => {
    render('/x/s1')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/spools/${spoolId(1)}`))
  })

  it('names an unknown code and takes another', async () => {
    const { user } = render('/x/Q-9')
    expect(await screen.findByRole('heading', { name: 'No such code: Q-9' })).toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'L-2')
    await user.click(screen.getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`/filament/locations/${L.SHELF_A}`))
  })

  it('searches the spools for a code that is not a ScadBuddy code, in its place in the history (#8372)', async () => {
    render('/x/6975373321234')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/filament/spools'))
    const query = new URLSearchParams(screen.getByTestId('query').textContent ?? '')
    expect(query.get('q')).toBe('gtin:6975373321234')
    expect(query.get('from')).toBe('scan')
    expect(screen.getByTestId('how')).toHaveTextContent('REPLACE')
  })

  it('keeps the sentence for a ScadBuddy code that is not found (#8372)', async () => {
    render('/x/S-999')
    expect(await screen.findByRole('heading', { name: 'No such code: S-999' })).toBeInTheDocument()
  })
})
