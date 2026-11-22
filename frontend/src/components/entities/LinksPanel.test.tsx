import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { entityMock, seedEntityLink } from '../../mocks/features/entities'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { LinksPanel } from './LinksPanel'

const TYPE = 'spool_type:7a000000-0000-4000-8000-000000000001'

describe('LinksPanel (#6950)', () => {
  beforeEach(() => resetMockState())

  it('adds a support link, opened in a new tab without the opener', async () => {
    const { user } = renderPage(<LinksPanel entity={TYPE} />)
    expect(await screen.findByText('No links yet.')).toBeInTheDocument()

    await user.type(screen.getByLabelText('Link address'), 'https://bambulab.com/support/pla')
    await choose(user, screen.getByRole('combobox', { name: 'Link kind' }), 'Support')
    await user.type(screen.getByLabelText('Link title'), 'PLA support page')
    await user.click(screen.getByRole('button', { name: 'Add link' }))

    const group = await screen.findByRole('list', { name: 'Support' })
    const link = within(group).getByRole('link', { name: 'PLA support page' })
    expect(link).toHaveAttribute('href', 'https://bambulab.com/support/pla')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(entityMock.links.get(TYPE)?.[0]).toMatchObject({ kind: 'support', title: 'PLA support page' })
  })

  it("says the server's bad_link_url refusal", async () => {
    const { user } = renderPage(<LinksPanel entity={TYPE} />)
    await screen.findByText('No links yet.')
    await user.type(screen.getByLabelText('Link address'), 'javascript:x')
    await user.click(screen.getByRole('button', { name: 'Add link' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('a link is an `http` or `https` URL with a host, up to 2000 characters')
    expect(entityMock.links.get(TYPE) ?? []).toHaveLength(0)
  })

  it('groups the links by kind, names an unknown kind as it is, and moves one within its group', async () => {
    seedEntityLink(TYPE, { url: 'https://a.example/one', kind: 'manual', title: 'One' })
    seedEntityLink(TYPE, { url: 'https://a.example/two', kind: 'manual', title: 'Two' })
    seedEntityLink(TYPE, { url: 'https://b.example/', kind: 'forum_thread' })
    const { user } = renderPage(<LinksPanel entity={TYPE} />)

    const manuals = await screen.findByRole('list', { name: 'Manual' })
    expect(within(manuals).getAllByRole('link').map((a) => a.textContent)).toEqual(['One', 'Two'])
    // No title: the host stands for it.
    expect(within(screen.getByRole('list', { name: 'forum_thread' })).getByRole('link', { name: 'b.example' })).toBeInTheDocument()

    await user.click(within(manuals).getByRole('button', { name: 'Move Two up' }))
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Manual' })).getAllByRole('link').map((a) => a.textContent)).toEqual(['Two', 'One']))
  })

  it('edits and removes a link', async () => {
    seedEntityLink(TYPE, { url: 'https://a.example/tds', kind: 'datasheet', title: 'TDS' })
    const { user } = renderPage(<LinksPanel entity={TYPE} />)
    const group = await screen.findByRole('list', { name: 'Datasheet' })

    await user.click(within(group).getByRole('button', { name: 'Edit TDS' }))
    const title = within(group).getByLabelText('Title of TDS')
    await user.clear(title)
    await user.type(title, 'Technical data')
    await user.click(within(group).getByRole('button', { name: 'Save' }))
    expect(await within(group).findByRole('link', { name: 'Technical data' })).toBeInTheDocument()
    expect(entityMock.links.get(TYPE)?.[0]).toMatchObject({ title: 'Technical data', source: 'manual' })

    await user.click(within(group).getByRole('button', { name: 'Remove Technical data' }))
    expect(await screen.findByText('No links yet.')).toBeInTheDocument()
  })
})
