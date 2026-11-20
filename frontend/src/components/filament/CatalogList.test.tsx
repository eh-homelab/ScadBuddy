import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { listCatalog } from '../../api/filament'
import { renderPage } from '../../test/utils'
import { CatalogList } from './CatalogList'

const row = (name: string) => screen.getByRole('row', { name: new RegExp(`^${name}\\b`) })

describe('the catalog aliases (#6706)', () => {
  it('shows each row’s aliases as chips beside its name', async () => {
    renderPage(<CatalogList kind="effects" />)
    const translucent = await screen.findByRole('row', { name: /^Translucent/ })
    expect(within(translucent).getByText('Transparent')).toBeInTheDocument()
  })

  it('adds and removes an alias from the edit form, and saves each change at once', async () => {
    const { user } = renderPage(<CatalogList kind="effects" />)
    await screen.findByRole('row', { name: /^Clear/ })
    await user.click(screen.getByRole('button', { name: 'Edit Clear' }))
    const form = screen.getByRole('form', { name: 'Edit Clear' })
    await user.type(within(form).getByRole('textbox', { name: 'New alias' }), ' See-through {Enter}')
    await within(form).findByRole('button', { name: 'Remove alias See-through' })
    expect((await listCatalog('effects')).find((one) => one.name === 'Clear')?.aliases).toEqual(['See-through'])

    await user.click(within(form).getByRole('button', { name: 'Remove alias See-through' }))
    await waitFor(() => expect(within(form).queryByRole('button', { name: 'Remove alias See-through' })).not.toBeInTheDocument())
    expect((await listCatalog('effects')).find((one) => one.name === 'Clear')?.aliases).toEqual([])
  })

  it('says inline which row holds a taken alias, and keeps the typed text', async () => {
    const { user } = renderPage(<CatalogList kind="effects" />)
    await screen.findByRole('row', { name: /^Clear/ })
    await user.click(screen.getByRole('button', { name: 'Edit Clear' }))
    const form = screen.getByRole('form', { name: 'Edit Clear' })
    const input = within(form).getByRole('textbox', { name: 'New alias' })
    await user.type(input, 'transparent{Enter}')
    expect(await within(form).findByText("the effect 'Translucent' already has a name or alias that matches 'transparent'")).toBeInTheDocument()
    expect(input).toHaveValue('transparent')
    expect(row('Clear')).not.toHaveTextContent('transparent')
  })

  it('offers no aliases in a catalog without them', async () => {
    const { user } = renderPage(<CatalogList kind="insert-materials" />)
    await screen.findByRole('row', { name: /^Plastic/ })
    await user.click(screen.getByRole('button', { name: 'Edit Plastic' }))
    expect(screen.queryByRole('textbox', { name: 'New alias' })).not.toBeInTheDocument()
  })
})
