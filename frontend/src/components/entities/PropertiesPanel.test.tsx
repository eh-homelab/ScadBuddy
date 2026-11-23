import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { resetMockState } from '../../mocks/handlers'
import { entityMock, seedEntityProperties } from '../../mocks/features/entities'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { PropertiesPanel } from './PropertiesPanel'

const TYPE = 'spool_type:7a000000-0000-4000-8000-000000000001'
const PROPERTIES = '/api/v1/entities/:ref/properties'

/** A gate a handler waits on, opened by the test. */
function gate() {
  let open!: () => void
  const opened = new Promise<void>((resolve) => (open = resolve))
  return { opened, open }
}

describe('PropertiesPanel (#7026)', () => {
  beforeEach(() => resetMockState())

  it("lists the entity's properties, a list as chips, with the schema's description", async () => {
    seedEntityProperties(TYPE, { mpn: 'A00-K0-1.75', gtin: ['0123', '0456'], colour_family: 'black' })
    renderPage(<PropertiesPanel entity={TYPE} />)

    const gtin = await screen.findByRole('row', { name: /gtin/ })
    expect(within(gtin).getAllByRole('listitem').map((chip) => chip.textContent)).toEqual(['0123', '0456'])
    expect(within(gtin).getByText('GTIN / EAN / UPC barcodes (spool and refill)')).toBeInTheDocument()
    const mpn = screen.getByRole('row', { name: /mpn/ })
    expect(within(mpn).getByText('A00-K0-1.75')).toBeInTheDocument()
    expect(within(mpn).getByText('Manufacturer part number')).toBeInTheDocument()
    // A key the schema does not know is shown as it is, with no hint.
    expect(within(screen.getByRole('row', { name: /colour_family/ })).getByText('black')).toBeInTheDocument()
  })

  it("adds a property in one PATCH, its type taken from the schema's", async () => {
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    expect(await screen.findByText('No properties yet.')).toBeInTheDocument()

    await user.type(screen.getByLabelText('New property key'), 'td')
    expect(screen.getByRole('combobox', { name: 'New property type' })).toHaveTextContent('Number')
    await user.type(screen.getByLabelText('New property value'), '1.8')
    await user.click(screen.getByRole('button', { name: 'Add property' }))

    expect(await screen.findByRole('row', { name: /td/ })).toHaveTextContent('1.8')
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { td: 1.8 } }])
  })

  it('adds a list item by item, never split at commas', async () => {
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await screen.findByText('No properties yet.')
    await user.type(screen.getByLabelText('New property key'), 'asin')
    expect(screen.getByRole('combobox', { name: 'New property type' })).toHaveTextContent('List')
    await user.type(screen.getByLabelText('New property value, item 1'), 'us:B0CXYZ1234, de')
    await user.click(screen.getByRole('button', { name: 'Add item' }))
    await user.type(screen.getByLabelText('New property value, item 2'), 'de:B0CXYZ9999')
    await user.click(screen.getByRole('button', { name: 'Add property' }))
    await screen.findByRole('row', { name: /asin/ })
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { asin: ['us:B0CXYZ1234, de', 'de:B0CXYZ9999'] } }])
  })

  it('edits a list with a comma in an item and sends it back exactly', async () => {
    seedEntityProperties(TYPE, { finish: ['Black, matte', 'x'] })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    const row = await screen.findByRole('row', { name: /finish/ })
    await user.click(within(row).getByRole('button', { name: 'Edit finish' }))
    expect(screen.getByLabelText('Value of finish, item 1')).toHaveValue('Black, matte')
    const second = screen.getByLabelText('Value of finish, item 2')
    await user.clear(second)
    await user.type(second, 'y')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('button', { name: 'Edit finish' })
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { finish: ['Black, matte', 'y'] } }])
  })

  it('sends the stored value unchanged on a rename, outer spaces and empty items kept', async () => {
    seedEntityProperties(TYPE, { tags: [' a', ''], note: '  padded  ' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    for (const [from, to, value] of [
      ['tags', 'labels', [' a', '']],
      ['note', 'remark', '  padded  '],
    ] as const) {
      const row = await screen.findByRole('row', { name: new RegExp(from) })
      await user.click(within(row).getByRole('button', { name: `Edit ${from}` }))
      const key = screen.getByLabelText(`Key of ${from}`)
      await user.clear(key)
      await user.type(key, to)
      await user.click(screen.getByRole('button', { name: 'Save' }))
      await screen.findByRole('button', { name: `Edit ${to}` })
      expect(entityMock.propertyPatches.at(-1)).toEqual({ entity: TYPE, set: { [from]: null, [to]: value } })
    }
  })

  it('keeps a boolean and a number their types when edited in place', async () => {
    seedEntityProperties(TYPE, { glossy: true, td: 1.8 })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /glossy/ })).getByRole('button', { name: 'Edit glossy' }))
    await choose(user, screen.getByRole('combobox', { name: 'Value of glossy' }), 'No')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await user.click(await screen.findByRole('button', { name: 'Edit td' }))
    const value = screen.getByLabelText('Value of td')
    await user.clear(value)
    await user.type(value, '2.1')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('button', { name: 'Edit td' })
    expect(entityMock.propertyPatches).toEqual([
      { entity: TYPE, set: { glossy: false } },
      { entity: TYPE, set: { td: 2.1 } },
    ])
  })

  it('asks before a rename replaces a key the entity has', async () => {
    seedEntityProperties(TYPE, { mpn: 'A', part: 'B' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /mpn/ })).getByRole('button', { name: 'Edit mpn' }))
    const key = screen.getByLabelText('Key of mpn')
    await user.clear(key)
    await user.type(key, 'part')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByRole('group', { name: 'Replace a property' })).toHaveTextContent('This replaces part’s value.')
    expect(entityMock.propertyPatches).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Replace' }))
    await waitFor(() => expect(screen.queryByRole('row', { name: /mpn/ })).toBeNull())
    expect(screen.getByRole('row', { name: /part/ })).toHaveTextContent('A')
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { mpn: null, part: 'A' } }])
  })

  it('asks before an add replaces a key the entity has, and keeps it on Keep it', async () => {
    seedEntityProperties(TYPE, { mpn: 'A' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await screen.findByRole('row', { name: /mpn/ })
    await user.type(screen.getByLabelText('New property key'), 'mpn')
    await user.type(screen.getByLabelText('New property value'), 'B')
    await user.click(screen.getByRole('button', { name: 'Add property' }))
    expect(screen.getByRole('group', { name: 'Replace a property' })).toHaveTextContent('This replaces mpn’s value.')
    await user.click(screen.getByRole('button', { name: 'Keep it' }))
    expect(screen.queryByRole('group', { name: 'Replace a property' })).toBeNull()
    expect(entityMock.propertyPatches).toEqual([])

    await user.click(screen.getByRole('button', { name: 'Add property' }))
    await user.click(screen.getByRole('button', { name: 'Replace' }))
    await waitFor(() => expect(screen.getByRole('row', { name: /mpn/ })).toHaveTextContent('B'))
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { mpn: 'B' } }])
  })

  it('renames a key in one PATCH: the old key null, the new one the value', async () => {
    seedEntityProperties(TYPE, { mpn: 'A00-K0' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    const row = await screen.findByRole('row', { name: /mpn/ })

    await user.click(within(row).getByRole('button', { name: 'Edit mpn' }))
    const key = screen.getByLabelText('Key of mpn')
    await user.clear(key)
    await user.type(key, 'part_number')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('row', { name: /part_number/ })).toHaveTextContent('A00-K0')
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { mpn: null, part_number: 'A00-K0' } }])
  })

  it("shows a 422's detail under the row", async () => {
    seedEntityProperties(TYPE, { td: 1.8 })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    const row = await screen.findByRole('row', { name: /td/ })

    await user.click(within(row).getByRole('button', { name: 'Edit td' }))
    await choose(user, screen.getByRole('combobox', { name: 'Type of td' }), 'Text')
    const value = screen.getByLabelText('Value of td')
    await user.clear(value)
    await user.type(value, 'thick')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('alert')).toHaveTextContent("property 'td': must be a number")
    expect(entityMock.properties.get(TYPE)?.[0]).toMatchObject({ key: 'td', value: 1.8 })
  })

  it('removes a property', async () => {
    seedEntityProperties(TYPE, { mpn: 'A00-K0' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    const row = await screen.findByRole('row', { name: /mpn/ })
    await user.click(within(row).getByRole('button', { name: 'Remove mpn' }))
    expect(await screen.findByText('No properties yet.')).toBeInTheDocument()
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { mpn: null } }])
  })

  it('takes a decimal number only: a hex or binary literal is an error, an exponent is not', async () => {
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await screen.findByText('No properties yet.')
    await user.type(screen.getByLabelText('New property key'), 'td')
    const value = screen.getByLabelText('New property value')
    for (const text of ['0x1A', '0b11', '0o7', '1.', '.5', 'Infinity']) {
      await user.clear(value)
      await user.type(value, text)
      await user.click(screen.getByRole('button', { name: 'Add property' }))
      expect(screen.getByRole('alert')).toHaveTextContent('Enter a number, such as 1.8.')
    }
    expect(entityMock.propertyPatches).toEqual([])
    await user.clear(value)
    await user.type(value, '-1.5e3')
    await user.click(screen.getByRole('button', { name: 'Add property' }))
    await screen.findByRole('row', { name: /td/ })
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { td: -1500 } }])
  })

  it('drops the empty items of a list on save, so an untouched new list saves as []', async () => {
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await screen.findByText('No properties yet.')
    await user.type(screen.getByLabelText('New property key'), 'asin')
    await user.click(screen.getByRole('button', { name: 'Add property' }))
    await screen.findByRole('row', { name: /asin/ })
    await user.type(screen.getByLabelText('New property key'), 'tags')
    await choose(user, screen.getByRole('combobox', { name: 'New property type' }), 'List')
    await user.type(screen.getByLabelText('New property value, item 1'), ' ')
    await user.click(screen.getByRole('button', { name: 'Add item' }))
    await user.click(screen.getByRole('button', { name: 'Add item' }))
    await user.type(screen.getByLabelText('New property value, item 3'), 'b')
    await user.click(screen.getByRole('button', { name: 'Add property' }))
    await screen.findByRole('row', { name: /tags/ })
    // Only an item that is exactly empty goes: a space is a value (strings are never trimmed).
    expect(entityMock.propertyPatches).toEqual([
      { entity: TYPE, set: { asin: [] } },
      { entity: TYPE, set: { tags: [' ', 'b'] } },
    ])
  })

  it("moves focus after a remove to the next row's Edit button, then the previous one's, then the add form", async () => {
    seedEntityProperties(TYPE, { a: '1', b: '2', c: '3' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /^b/ })).getByRole('button', { name: 'Remove b' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit c' })).toHaveFocus())
    await user.click(screen.getByRole('button', { name: 'Remove c' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit a' })).toHaveFocus())
    await user.click(screen.getByRole('button', { name: 'Remove a' }))
    await waitFor(() => expect(screen.getByLabelText('New property key')).toHaveFocus())
  })

  it('offers no add form while the properties load, so a replace cannot go unasked', async () => {
    seedEntityProperties(TYPE, { gtin: ['1', '2', '3'] })
    const read = gate()
    server.use(
      http.get(PROPERTIES, async () => {
        await read.opened
      }),
    )
    renderPage(<PropertiesPanel entity={TYPE} />)
    expect(await screen.findByText('Loading the properties…')).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Add a property' })).toBeNull()
    expect(screen.queryByLabelText('New property key')).toBeNull()
    read.open()
    expect(await screen.findByRole('form', { name: 'Add a property' })).toBeInTheDocument()
  })

  it('offers no add form when the properties could not be loaded', async () => {
    seedEntityProperties(TYPE, { gtin: ['1', '2', '3'] })
    server.use(http.get(PROPERTIES, () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    renderPage(<PropertiesPanel entity={TYPE} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('The properties could not be loaded')
    expect(screen.queryByRole('form', { name: 'Add a property' })).toBeNull()
    expect(screen.queryByLabelText('New property key')).toBeNull()
  })

  it('sends one PATCH at a time: every other write waits until it answers', async () => {
    seedEntityProperties(TYPE, { mpn: 'A', part: 'B' })
    const reply = gate()
    server.use(
      http.patch(PROPERTIES, async () => {
        await reply.opened
      }),
    )
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /mpn/ })).getByRole('button', { name: 'Remove mpn' }))
    const part = screen.getByRole('row', { name: /^part/ })
    expect(within(part).getByRole('button', { name: 'Remove part' })).toBeDisabled()
    expect(within(part).getByRole('button', { name: 'Edit part' })).toBeDisabled()
    await user.type(screen.getByLabelText('New property key'), 'td')
    expect(screen.getByRole('button', { name: 'Add property' })).toBeDisabled()
    reply.open()
    await waitFor(() => expect(screen.queryByRole('row', { name: /mpn/ })).toBeNull())
    expect(within(screen.getByRole('row', { name: /^part/ })).getByRole('button', { name: 'Remove part' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Add property' })).toBeEnabled()
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { mpn: null } }])
  })

  it('drops a reply that lands after the panel moved to another entity', async () => {
    const other = 'spool_type:7a000000-0000-4000-8000-000000000002'
    seedEntityProperties(TYPE, { mpn: 'A' })
    seedEntityProperties(other, { part: 'B' })
    const reply = gate()
    server.use(
      http.patch(PROPERTIES, async () => {
        await reply.opened
        return HttpResponse.json({ properties: [{ key: 'mpn', value: 'A2', source: 'manual', updated_at: '2026-11-23T00:00:00Z', updated_by: null }] })
      }),
    )
    const { user, rerender } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /mpn/ })).getByRole('button', { name: 'Remove mpn' }))
    rerender(<PropertiesPanel entity={other} />)
    expect(await screen.findByRole('row', { name: /^part/ })).toBeInTheDocument()
    reply.open()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByRole('row', { name: /mpn/ })).toBeNull()
    expect(screen.getByRole('row', { name: /^part/ })).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /^part/ })).getByRole('button', { name: 'Remove part' })).toBeEnabled()
  })

  it("moves focus into a row's form, and back to its Edit button on Cancel, Escape and Save", async () => {
    seedEntityProperties(TYPE, { mpn: 'A' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /mpn/ })).getByRole('button', { name: 'Edit mpn' }))
    expect(screen.getByLabelText('Key of mpn')).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Edit mpn' })).toHaveFocus()

    await user.keyboard('{Enter}')
    expect(screen.getByLabelText('Key of mpn')).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByLabelText('Key of mpn')).toBeNull()
    expect(screen.getByRole('button', { name: 'Edit mpn' })).toHaveFocus()
    expect(entityMock.propertyPatches).toEqual([])

    await user.keyboard('{Enter}')
    await user.type(screen.getByLabelText('Value of mpn'), 'B{Enter}')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit mpn' })).toHaveFocus())
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { mpn: 'AB' } }])
  })

  it('returns focus to the renamed row after a rename', async () => {
    seedEntityProperties(TYPE, { mpn: 'A' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /mpn/ })).getByRole('button', { name: 'Edit mpn' }))
    await user.clear(screen.getByLabelText('Key of mpn'))
    await user.type(screen.getByLabelText('Key of mpn'), 'part{Enter}')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit part' })).toHaveFocus())
  })

  it('keeps the form open when Escape only closes its type menu', async () => {
    seedEntityProperties(TYPE, { mpn: 'A' })
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await user.click(within(await screen.findByRole('row', { name: /mpn/ })).getByRole('button', { name: 'Edit mpn' }))
    await user.click(screen.getByRole('combobox', { name: 'Type of mpn' }))
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(screen.getByLabelText('Key of mpn')).toBeInTheDocument()
  })
})
