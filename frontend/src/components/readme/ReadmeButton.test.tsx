import { readFileSync } from 'node:fs'
import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ReadmeButton } from './ReadmeButton'

/** The real built-in's README, whose images are relative to its folder. */
const chunkyReadme = readFileSync(`${import.meta.dirname}/../../../../models/chunky-name-sign/README.md`, 'utf8')

describe('ReadmeButton (#1074)', () => {
  it("renders a template's README with its relative images loading through the model's image route", async () => {
    server.use(
      http.get('/api/v1/models/chunky-name-sign/readme', () =>
        HttpResponse.text(chunkyReadme, { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } }),
      ),
    )
    const { user } = renderPage(<ReadmeButton slug="chunky-name-sign" />)

    await user.click(screen.getByRole('button', { name: 'README' }))
    const dialog = screen.getByRole('dialog', { name: 'README' })
    const cover = await within(dialog).findByRole('img', {
      name: 'Chunky name sign rendered with the default parameters',
    })
    expect(cover).toHaveAttribute('src', '/api/v1/models/chunky-name-sign/images/thumbnail.png')
    expect(within(dialog).getByRole('img', { name: 'Circle badge with an outer ring' })).toHaveAttribute(
      'src',
      '/api/v1/models/chunky-name-sign/images/images/circle-badge.png',
    )
  })

  it('says so when the template has no README', async () => {
    server.use(
      http.get('/api/v1/models/:slug/readme', () =>
        HttpResponse.json({ status: 404, title: 'Not Found', detail: 'no README' }, { status: 404 }),
      ),
    )
    const { user } = renderPage(<ReadmeButton slug="plain" />)

    await user.click(screen.getByRole('button', { name: 'README' }))
    expect(await screen.findByText('This template has no README.')).toBeInTheDocument()
  })

  it('says why when the README cannot be read', async () => {
    server.use(
      http.get('/api/v1/models/:slug/readme', () =>
        HttpResponse.json({ status: 500, title: 'Internal Server Error', detail: 'disk gone' }, { status: 500 }),
      ),
    )
    const { user } = renderPage(<ReadmeButton slug="plain" />)

    await user.click(screen.getByRole('button', { name: 'README' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the README: disk gone')
  })
})
