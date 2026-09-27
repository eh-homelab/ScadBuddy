import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { CataloguePage } from '../pages/CataloguePage'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { DeleteModelButton } from './DeleteModelButton'

function render() {
  return renderPage(
    <Routes>
      <Route path="/" element={<CataloguePage />} />
      <Route
        path="/m/:slug"
        element={<DeleteModelButton slug="name-keychain" name="Name Keychain" />}
      />
    </Routes>,
    { route: '/m/name-keychain' },
  )
}

describe('DeleteModelButton', () => {
  it('asks first, naming the model, and deletes nothing on cancel', async () => {
    const deletes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') deletes.push(new URL(request.url).pathname)
    })
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete Name Keychain?' })
    expect(dialog).toHaveTextContent('name-keychain')

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(deletes).toEqual([])
  })

  it('deletes on confirm and lands on a refreshed model list', async () => {
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Name Keychain' })).not.toBeInTheDocument()
  })

  it('stays put and says why when the server refuses', async () => {
    server.use(
      http.delete('/api/v1/models/:slug', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: "'name-keychain' has a render in progress; try again when it ends",
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Delete model' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('render in progress')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Delete model' })).toBeEnabled(),
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
