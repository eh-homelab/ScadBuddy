import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { NATIVE_SPOOL_RED } from '../../mocks/features/nativeSpools'
import { resetMockState } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { NativeSpools } from './NativeSpools'

// #4643 — ScadBuddy's own spools, under Settings' printing area.
describe('NativeSpools (#4643)', () => {
  beforeEach(() => resetMockState())

  it('lists, adds, edits and archives the spools', async () => {
    const { user } = renderPage(<NativeSpools />)
    const table = await screen.findByRole('table', { name: "ScadBuddy's spools" })
    const red = await within(table).findByTestId(`native-spool-${NATIVE_SPOOL_RED}`)
    expect(within(red).getByText('Bambu PLA Red')).toBeInTheDocument()
    expect(within(red).getByText('812 g of 1000 g')).toBeInTheDocument()
    expect(within(red).getByText('RFID')).toBeInTheDocument()

    const form = screen.getByRole('form', { name: 'Add a spool' })
    await user.type(within(form).getByLabelText('Material'), 'PETG')
    await user.type(within(form).getByLabelText('Colour'), '#00ff00')
    await user.type(within(form).getByLabelText('Grams left'), '900')
    await user.click(within(form).getByRole('button', { name: 'Add spool' }))
    await waitFor(async () => expect((await api.nativeSpools()).map((s) => [s.material, s.colour, s.remaining_g])).toContainEqual(['PETG', '#00FF00', 900]))
    await within(await screen.findByRole('table', { name: "ScadBuddy's spools" })).findByText('PETG')

    await user.click(within(screen.getByTestId(`native-spool-${NATIVE_SPOOL_RED}`)).getByRole('button', { name: 'Edit' }))
    const edit = screen.getByRole('form', { name: 'Edit spool' })
    await user.clear(within(edit).getByLabelText('Grams left'))
    await user.type(within(edit).getByLabelText('Grams left'), '650')
    await user.clear(within(edit).getByLabelText('Brand'))
    await user.click(within(edit).getByRole('button', { name: 'Save spool' }))
    await waitFor(async () => {
      const spool = (await api.nativeSpools()).find((s) => s.id === NATIVE_SPOOL_RED)
      expect([spool?.remaining_g, spool?.brand, spool?.tray_uuid]).toEqual([650, null, 'A1B2C3D4E5F60718293A4B5C6D7E8F90'])
    })

    await user.click(within(screen.getByTestId(`native-spool-${NATIVE_SPOOL_RED}`)).getByRole('button', { name: /^Archive/ }))
    await waitFor(() => expect(screen.queryByTestId(`native-spool-${NATIVE_SPOOL_RED}`)).toBeNull())
  })

  it('unarchives and deletes an archived spool', async () => {
    await api.updateNativeSpool(NATIVE_SPOOL_RED, { archived: true })
    const { user } = renderPage(<NativeSpools />)
    await screen.findByText('No spools yet. Add the ones on your shelf.')
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    const row = await screen.findByTestId(`native-spool-${NATIVE_SPOOL_RED}`)
    expect(within(row).queryByRole('button', { name: 'Edit' })).toBeNull()
    await user.click(within(row).getByRole('button', { name: /^Unarchive/ }))
    await waitFor(async () => expect((await api.nativeSpools()).map((s) => s.id)).toEqual([NATIVE_SPOOL_RED]))

    await api.updateNativeSpool(NATIVE_SPOOL_RED, { archived: true })
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    await user.click(within(await screen.findByTestId(`native-spool-${NATIVE_SPOOL_RED}`)).getByRole('button', { name: /^Delete/ }))
    await waitFor(async () => expect(await api.nativeSpools(true)).toEqual([]))
  })

  it('refuses a colour no spool has, and says why a tag is taken', async () => {
    const { user } = renderPage(<NativeSpools />)
    await screen.findByTestId(`native-spool-${NATIVE_SPOOL_RED}`)
    const form = screen.getByRole('form', { name: 'Add a spool' })
    await user.type(within(form).getByLabelText('Colour'), 'red')
    await user.click(within(form).getByRole('button', { name: 'Add spool' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter the colour as #RRGGBB')

    await user.clear(within(form).getByLabelText('Colour'))
    await user.type(within(form).getByLabelText('Tray UUID'), 'a1b2c3d4e5f60718293a4b5c6d7e8f90')
    await user.click(within(form).getByRole('button', { name: 'Add spool' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('another spool on the shelf carries that tag')
  })
})
