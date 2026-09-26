import { screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ModelLibrariesButton } from './ModelLibrariesButton'

describe('ModelLibrariesButton', () => {
  it('offers only the libraries that are pinned, and saves the declaration', async () => {
    const patches: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'PATCH') patches.push(await request.clone().json())
    })
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)

    await user.click(screen.getByRole('button', { name: 'Libraries' }))
    const bosl2 = await screen.findByRole('checkbox', { name: /BOSL2/ })
    expect(bosl2).not.toBeChecked()
    expect(screen.queryByRole('checkbox', { name: /dotSCAD/ })).not.toBeInTheDocument()

    await user.click(bosl2)
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('button', { name: /^Libraries\s*1$/ })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(patches).toEqual([{ libraries: ['BOSL2'] }])
  })

  it('points at the Libraries page when nothing is pinned yet', async () => {
    server.use(http.get('/api/v1/libraries', () => HttpResponse.json([])))
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)

    await user.click(screen.getByRole('button', { name: 'Libraries' }))

    expect(await screen.findByRole('link', { name: 'Libraries page' })).toHaveAttribute(
      'href',
      '/libraries',
    )
  })

  it('stays open and says why when the server refuses', async () => {
    server.use(
      http.patch('/api/v1/models/:slug', () =>
        HttpResponse.json(
          { title: 'Unprocessable Content', status: 422, detail: 'not added yet: BOSL2' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<ModelLibrariesButton slug="name-keychain" name="Name Keychain" />)

    await user.click(screen.getByRole('button', { name: 'Libraries' }))
    await user.click(await screen.findByRole('checkbox', { name: /BOSL2/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('not added yet: BOSL2')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
