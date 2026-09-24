import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { models } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { CataloguePage } from './CataloguePage'

describe('CataloguePage', () => {
  it('lists every model with its tags and when it last changed', async () => {
    renderPage(<CataloguePage />)

    const keychain = await screen.findByRole('heading', { name: 'Name Keychain' })
    const card = keychain.closest('li') as HTMLElement
    expect(within(card).getByText('keychain')).toBeInTheDocument()
    expect(within(card).getByText(/^Updated /)).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
  })

  it('shows an empty build plate for a model with no thumbnail', async () => {
    renderPage(<CataloguePage />)
    const gridfinity = await screen.findByRole('heading', { name: 'Gridfinity Bin' })
    const card = gridfinity.closest('li') as HTMLElement
    expect(
      within(card).getByRole('img', { name: 'Gridfinity Bin — not generated yet' }),
    ).toBeInTheDocument()
  })

  it('links each card at the customizer', async () => {
    renderPage(<CataloguePage />)
    const link = await screen.findByRole('link', { name: /Name Keychain/ })
    expect(link).toHaveAttribute('href', '/m/name-keychain')
  })

  it('points at the models folder when there is nothing to show', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.json([])))
    renderPage(<CataloguePage />)

    expect(await screen.findByRole('heading', { name: 'No models yet' })).toBeInTheDocument()
    expect(screen.getByText('models/')).toBeInTheDocument()
  })

  it('reports a failed catalogue load and offers a retry', async () => {
    server.use(
      http.get('/api/v1/models', () =>
        HttpResponse.json({ title: 'Data directory is unreadable', status: 500 }, { status: 500 }),
      ),
    )
    renderPage(<CataloguePage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Data directory is unreadable')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('uploads a chosen .scad file and closes the dialog', async () => {
    // The multipart POST itself is exercised by the Playwright smoke test: jsdom's
    // Blob/File and Node's fetch cannot agree on a multipart body, which fails
    // inside undici rather than in any ScadBuddy code.
    const uploaded = { ...(models[0] as (typeof models)[number]), slug: 'vase-mode', name: 'Vase Mode' }
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)

    const { user } = renderPage(<CataloguePage />)
    await screen.findByRole('heading', { name: 'Name Keychain' })

    await user.click(screen.getByRole('button', { name: 'Add model' }))
    const dialog = screen.getByRole('dialog')
    await user.upload(
      within(dialog).getByLabelText('OpenSCAD source file'),
      new File(['cube(10);'], 'Vase Mode.scad', { type: 'text/plain' }),
    )
    expect(within(dialog).getByText('Vase Mode.scad')).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(upload).toHaveBeenCalledOnce()
    expect(upload.mock.calls[0]?.[0].name).toBe('Vase Mode.scad')
    upload.mockRestore()
  })

  it('refuses anything that is not a .scad file', async () => {
    const { user } = renderPage(<CataloguePage />, {
      userEventOptions: { applyAccept: false },
    })
    await screen.findByRole('heading', { name: 'Name Keychain' })

    await user.click(screen.getByRole('button', { name: 'Add model' }))
    await user.upload(
      screen.getByLabelText('OpenSCAD source file'),
      new File(['x'], 'model.stl', { type: 'model/stl' }),
    )

    expect(screen.getByRole('alert')).toHaveTextContent('not a .scad file')
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: 'Add model' })).toBeDisabled()
  })
})
