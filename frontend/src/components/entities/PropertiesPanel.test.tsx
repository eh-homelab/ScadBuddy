import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { entityMock, seedEntityProperties } from '../../mocks/features/entities'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { PropertiesPanel } from './PropertiesPanel'

const TYPE = 'spool_type:7a000000-0000-4000-8000-000000000001'

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
})
