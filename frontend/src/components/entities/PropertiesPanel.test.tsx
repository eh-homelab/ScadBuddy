import { screen, within } from '@testing-library/react'
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

  it('adds a list, split at commas', async () => {
    const { user } = renderPage(<PropertiesPanel entity={TYPE} />)
    await screen.findByText('No properties yet.')
    await user.type(screen.getByLabelText('New property key'), 'asin')
    expect(screen.getByRole('combobox', { name: 'New property type' })).toHaveTextContent('List')
    await user.type(screen.getByLabelText('New property value'), 'us:B0CXYZ1234, de:B0CXYZ9999')
    await user.click(screen.getByRole('button', { name: 'Add property' }))
    await screen.findByRole('row', { name: /asin/ })
    expect(entityMock.propertyPatches).toEqual([{ entity: TYPE, set: { asin: ['us:B0CXYZ1234', 'de:B0CXYZ9999'] } }])
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
