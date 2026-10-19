import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { isUserOnly } from '../../agent/dom'
import { PROBE_FAILED, UNREACHABLE, printersState } from '../../mocks/features/printers'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { PrintersPanel } from './Printers'

const H2C = 'Printer 3DP-31B-598'
const GARAGE = 'Printer Garage'

describe('PrintersPanel (#3823)', () => {
  it('lists printers with their links and path', async () => {
    renderPage(<PrintersPanel />)

    const h2c = await screen.findByRole('listitem', { name: H2C })
    expect(h2c).toHaveTextContent('01P00A000000001')
    expect(h2c).toHaveTextContent('H2C')
    expect(h2c).toHaveTextContent('Bambuddy printer 1')
    expect(h2c).toHaveTextContent('No LAN link')
    expect(within(h2c).getByRole('combobox', { name: 'How 3DP-31B-598 prints' })).toHaveTextContent('Through Bambuddy')

    const garage = screen.getByRole('listitem', { name: GARAGE })
    expect(garage).toHaveTextContent('Not linked to Bambuddy')
    expect(within(garage).getByRole('combobox', { name: 'How Garage prints' })).toHaveTextContent('Automatic')
  })

  it("sets a printer's path", async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })

    await choose(user, within(garage).getByRole('combobox', { name: 'How Garage prints' }), 'Over the LAN')

    await waitFor(() => expect(printersState()[1]!.preferred_path).toBe('native'))
  })

  it('links a discovered Bambuddy printer and disables one without a serial', async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })

    const spare = await within(garage).findByRole('button', { name: 'Link Bambuddy printer Spare' })
    expect(spare).toBeDisabled()
    expect(garage).toHaveTextContent('Bambuddy did not report a serial')
    // The one already linked to another printer is not offered.
    expect(within(garage).queryByRole('button', { name: 'Link Bambuddy printer 3DP-31B-598' })).toBeNull()

    await user.click(within(garage).getByRole('button', { name: 'Link Bambuddy printer Workshop' }))

    await waitFor(() => expect(garage).toHaveTextContent('Bambuddy printer 2'))
    expect(printersState()[1]!.bambuddy_printer_id).toBe(2)

    await user.click(within(garage).getByRole('button', { name: 'Unlink Bambuddy from Garage' }))
    await waitFor(() => expect(garage).toHaveTextContent('Not linked to Bambuddy'))
  })

  it('links a LAN printer and never shows the code again', async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })
    const form = within(garage).getByRole('form', { name: 'LAN link for Garage' })
    expect(isUserOnly(form)).toBe(true)

    await user.type(within(form).getByLabelText('Host or IP address'), '10.0.0.9')
    await user.type(within(form).getByLabelText('Access code'), '12345678')
    expect(within(form).getByLabelText('Access code')).toHaveAttribute('type', 'password')
    await user.click(within(form).getByRole('button', { name: 'Test and link' }))

    await waitFor(() => expect(garage).toHaveTextContent('LAN 10.0.0.9'))
    expect(document.body.textContent).not.toContain('12345678')
    for (const input of document.querySelectorAll('input')) expect(input.value).not.toBe('12345678')
    expect(printersState()[1]!.has_access_code).toBe(true)

    await user.click(within(garage).getByRole('button', { name: 'Unlink the LAN from Garage' }))
    await waitFor(() => expect(garage).toHaveTextContent('No LAN link'))
  })

  it('shows the probe failure from a 502', async () => {
    const { user } = renderPage(<PrintersPanel />)
    const garage = await screen.findByRole('listitem', { name: GARAGE })
    const form = within(garage).getByRole('form', { name: 'LAN link for Garage' })

    await user.type(within(form).getByLabelText('Host or IP address'), UNREACHABLE)
    await user.type(within(form).getByLabelText('Access code'), '12345678')
    await user.click(within(form).getByRole('button', { name: 'Test and link' }))

    expect(await within(form).findByRole('alert')).toHaveTextContent(PROBE_FAILED)
    expect(printersState()[1]!.has_access_code).toBe(false)
  })

  it('adds a printer by its serial and forgets one', async () => {
    const { user } = renderPage(<PrintersPanel />)
    await screen.findByRole('listitem', { name: GARAGE })
    const form = screen.getByRole('form', { name: 'Add a printer' })

    await user.type(within(form).getByLabelText('Serial'), '01P00A0000000BB')
    await user.type(within(form).getByLabelText('Name'), 'Attic')
    await user.click(within(form).getByRole('button', { name: 'Add printer' }))

    const attic = await screen.findByRole('listitem', { name: 'Printer Attic' })
    await user.click(within(attic).getByRole('button', { name: 'Forget Attic' }))
    await waitFor(() => expect(screen.queryByRole('listitem', { name: 'Printer Attic' })).toBeNull())
  })

  it("says why a Bambuddy-linked printer can't be forgotten", async () => {
    const { user } = renderPage(<PrintersPanel />)
    const h2c = await screen.findByRole('listitem', { name: H2C })

    await user.click(within(h2c).getByRole('button', { name: 'Forget 3DP-31B-598' }))

    expect(await within(h2c).findByRole('alert')).toHaveTextContent('unlink it first')
  })
})
